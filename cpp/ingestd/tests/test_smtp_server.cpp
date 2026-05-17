#include "../src/domain_cache.h"
#include "../src/mail_queue.h"
#include "../src/smtp_server.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstring>
#include <future>
#include <stdexcept>
#include <string>
#include <thread>

namespace test {
void check(bool condition, const std::string& message);
}

namespace {

using namespace std::chrono_literals;

void close_fd(int fd) {
    if (fd >= 0) {
        (void)::close(fd);
    }
}

std::runtime_error socket_error(const std::string& action) {
    return std::runtime_error(action + ": " + std::strerror(errno));
}

int reserve_loopback_port() {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        throw socket_error("socket");
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(0);
    if (::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) {
        const int saved_errno = errno;
        close_fd(fd);
        errno = saved_errno;
        throw socket_error("bind");
    }

    socklen_t address_size = sizeof(address);
    if (::getsockname(fd, reinterpret_cast<sockaddr*>(&address), &address_size) < 0) {
        const int saved_errno = errno;
        close_fd(fd);
        errno = saved_errno;
        throw socket_error("getsockname");
    }

    const int port = ntohs(address.sin_port);
    close_fd(fd);
    return port;
}

int connect_loopback(int port) {
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(static_cast<uint16_t>(port));

    for (int attempt = 0; attempt < 50; ++attempt) {
        const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
        if (fd < 0) {
            throw socket_error("socket");
        }
        if (::connect(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0) {
            return fd;
        }

        const int saved_errno = errno;
        close_fd(fd);
        if (saved_errno != ECONNREFUSED && saved_errno != EINTR) {
            errno = saved_errno;
            throw socket_error("connect");
        }
        std::this_thread::sleep_for(10ms);
    }

    throw std::runtime_error("connect timed out");
}

std::string recv_line_with_timeout(int fd, std::chrono::milliseconds timeout) {
    std::string line;
    const auto deadline = std::chrono::steady_clock::now() + timeout;

    for (;;) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            throw std::runtime_error("recv line timed out");
        }

        const auto remaining =
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
        pollfd poll_fd{.fd = fd, .events = POLLIN, .revents = 0};
        const int ready = ::poll(&poll_fd, 1, static_cast<int>(remaining.count()));
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw socket_error("poll");
        }
        if (ready == 0) {
            throw std::runtime_error("recv line timed out");
        }

        char ch = '\0';
        const ssize_t received = ::recv(fd, &ch, 1, 0);
        if (received == 0) {
            throw std::runtime_error("connection closed while reading line");
        }
        if (received < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw socket_error("recv");
        }
        if (ch == '\n') {
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            return line;
        }
        line.push_back(ch);
    }
}

void recv_eof_with_timeout(int fd, std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;

    for (;;) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            throw std::runtime_error("recv eof timed out");
        }

        const auto remaining =
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
        pollfd poll_fd{.fd = fd, .events = POLLIN, .revents = 0};
        const int ready = ::poll(&poll_fd, 1, static_cast<int>(remaining.count()));
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw socket_error("poll");
        }
        if (ready == 0) {
            throw std::runtime_error("recv eof timed out");
        }

        char ch = '\0';
        const ssize_t received = ::recv(fd, &ch, 1, 0);
        if (received == 0) {
            return;
        }
        if (received < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw socket_error("recv");
        }
        throw std::runtime_error("received data while waiting for eof");
    }
}

}  // namespace

void test_smtp_server_stop_wakes_idle_client() {
    rapid_inbox::ingestd::DomainCache domains("/tmp/rapid-inbox-smtp-server-test.sqlite", 5000);
    rapid_inbox::ingestd::MailQueue queue(10);
    const int port = reserve_loopback_port();
    rapid_inbox::ingestd::SmtpServer server(
        "127.0.0.1", port, domains, queue, 20, 1024 * 1024, 30);

    server.start();
    int client_fd = -1;
    try {
        client_fd = connect_loopback(port);
        const std::string greeting = recv_line_with_timeout(client_fd, 1s);
        test::check(greeting == "220 rapid-inbox-ingestd", "smtp server greeting");

        auto stop_future = std::async(std::launch::async, [&server] { server.stop(); });
        if (stop_future.wait_for(300ms) != std::future_status::ready) {
            (void)::shutdown(client_fd, SHUT_RDWR);
            close_fd(client_fd);
            client_fd = -1;
            test::check(stop_future.wait_for(2s) == std::future_status::ready,
                        "smtp server stop remained blocked after client cleanup");
            stop_future.get();
            throw std::runtime_error("smtp server stop timed out with an idle client");
        }

        stop_future.get();
        close_fd(client_fd);
        client_fd = -1;
    } catch (...) {
        if (client_fd >= 0) {
            (void)::shutdown(client_fd, SHUT_RDWR);
            close_fd(client_fd);
        }
        server.stop();
        throw;
    }
}

void test_smtp_server_idle_client_times_out() {
    rapid_inbox::ingestd::DomainCache domains("/tmp/rapid-inbox-smtp-server-timeout.sqlite", 5000);
    rapid_inbox::ingestd::MailQueue queue(10);
    const int port = reserve_loopback_port();
    rapid_inbox::ingestd::SmtpServer server("127.0.0.1", port, domains, queue, 20, 1024 * 1024, 1);

    server.start();
    int client_fd = -1;
    try {
        client_fd = connect_loopback(port);
        const std::string greeting = recv_line_with_timeout(client_fd, 1s);
        test::check(greeting == "220 rapid-inbox-ingestd", "smtp server timeout greeting");

        recv_eof_with_timeout(client_fd, 3s);
        close_fd(client_fd);
        client_fd = -1;
        server.stop();
    } catch (...) {
        if (client_fd >= 0) {
            (void)::shutdown(client_fd, SHUT_RDWR);
            close_fd(client_fd);
        }
        server.stop();
        throw;
    }
}
