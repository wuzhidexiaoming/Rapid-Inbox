#include "smtp_server.h"

#include "smtp_session.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

namespace rapid_inbox::ingestd {
namespace {

std::runtime_error socket_error(const std::string& action) {
    return std::runtime_error(action + ": " + std::strerror(errno));
}

void close_fd(int fd) {
    if (fd >= 0) {
        (void)::close(fd);
    }
}

bool send_all(int fd, const char* data, std::size_t size) {
    std::size_t sent_total = 0;
    while (sent_total < size) {
        const ssize_t sent =
            ::send(fd, data + sent_total, size - sent_total, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR) {
                continue;
            }
            return false;
        }
        if (sent == 0) {
            return false;
        }
        sent_total += static_cast<std::size_t>(sent);
    }
    return true;
}

bool send_line(int fd, const std::string& line) {
    const std::string payload = line + "\r\n";
    return send_all(fd, payload.data(), payload.size());
}

class ClientLineReader {
public:
    explicit ClientLineReader(int fd) : fd_(fd) {}

    bool recv_line(std::string& line) {
        line.clear();
        for (;;) {
            if (cursor_ == end_ && !fill()) {
                return false;
            }

            const char ch = buffer_[cursor_++];
            if (ch == '\n') {
                if (!line.empty() && line.back() == '\r') {
                    line.pop_back();
                }
                return true;
            }
            line.push_back(ch);
        }
    }

private:
    bool fill() {
        for (;;) {
            const ssize_t received = ::recv(fd_, buffer_.data(), buffer_.size(), 0);
            if (received == 0) {
                return false;
            }
            if (received < 0) {
                if (errno == EINTR) {
                    continue;
                }
                return false;
            }
            cursor_ = 0;
            end_ = static_cast<std::size_t>(received);
            return true;
        }
    }

    int fd_;
    std::array<char, 4096> buffer_{};
    std::size_t cursor_ = 0;
    std::size_t end_ = 0;
};

int create_listen_socket(const std::string& host, int port) {
    if (port < 0 || port > 65535) {
        throw std::runtime_error("invalid SMTP_PORT: " + std::to_string(port));
    }

    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        throw socket_error("socket");
    }

    const int enabled = 1;
    if (::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0) {
        const int saved_errno = errno;
        close_fd(fd);
        errno = saved_errno;
        throw socket_error("setsockopt(SO_REUSEADDR)");
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(port));
    if (host.empty()) {
        address.sin_addr.s_addr = htonl(INADDR_ANY);
    } else if (::inet_pton(AF_INET, host.c_str(), &address.sin_addr) != 1) {
        close_fd(fd);
        throw std::runtime_error("invalid SMTP_HOST IPv4 address: " + host);
    }

    if (::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) {
        const int saved_errno = errno;
        close_fd(fd);
        errno = saved_errno;
        throw socket_error("bind");
    }

    if (::listen(fd, 1024) < 0) {
        const int saved_errno = errno;
        close_fd(fd);
        errno = saved_errno;
        throw socket_error("listen");
    }

    return fd;
}

}  // namespace

SmtpServer::SmtpServer(std::string host,
                       int port,
                       DomainCache& domains,
                       MailQueue& queue,
                       int max_recipients,
                       int max_message_size_bytes)
    : host_(std::move(host)),
      port_(port),
      domains_(domains),
      queue_(queue),
      max_recipients_(max_recipients),
      max_message_size_bytes_(max_message_size_bytes) {
    if (max_message_size_bytes_ < 0) {
        throw std::runtime_error("invalid MAX_MESSAGE_SIZE_BYTES: " +
                                 std::to_string(max_message_size_bytes_));
    }
}

SmtpServer::~SmtpServer() {
    stop();
}

void SmtpServer::start() {
    bool expected = false;
    if (!running_.compare_exchange_strong(expected, true)) {
        return;
    }

    try {
        listen_fd_ = create_listen_socket(host_, port_);
        accept_thread_ = std::thread([this] { accept_loop(); });
    } catch (...) {
        running_ = false;
        close_fd(listen_fd_);
        listen_fd_ = -1;
        throw;
    }
}

void SmtpServer::stop() {
    running_ = false;

    if (listen_fd_ >= 0) {
        (void)::shutdown(listen_fd_, SHUT_RDWR);
        close_fd(listen_fd_);
        listen_fd_ = -1;
    }
    shutdown_active_clients();

    if (accept_thread_.joinable()) {
        accept_thread_.join();
    }

    shutdown_active_clients();
    for (std::thread& thread : client_threads_) {
        if (thread.joinable()) {
            thread.join();
        }
    }
    client_threads_.clear();
}

void SmtpServer::accept_loop() {
    const int fd = listen_fd_;
    while (running_) {
        const int client_fd = ::accept(fd, nullptr, nullptr);
        if (client_fd < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (!running_) {
                break;
            }
            continue;
        }

        if (!running_) {
            close_fd(client_fd);
            break;
        }

        try {
            register_client_fd(client_fd);
            client_threads_.emplace_back([this, client_fd] { handle_client(client_fd); });
        } catch (...) {
            close_client_fd(client_fd);
        }
    }
}

void SmtpServer::handle_client(int client_fd) {
    auto domain_rules = domains_.snapshot_rules();
    SmtpSession session(domain_rules.matcher,
                        queue_,
                        max_recipients_,
                        static_cast<std::size_t>(max_message_size_bytes_),
                        std::move(domain_rules.policies));

    if (!send_line(client_fd, session.greeting())) {
        close_client_fd(client_fd);
        return;
    }

    ClientLineReader reader(client_fd);
    std::string line;
    while (running_ && reader.recv_line(line)) {
        std::string response = session.handle_line(line);
        if (response.empty()) {
            continue;
        }
        if (!send_line(client_fd, response)) {
            break;
        }
        if (response.rfind("221", 0) == 0) {
            break;
        }
    }

    close_client_fd(client_fd);
}

void SmtpServer::register_client_fd(int client_fd) {
    const std::lock_guard lock(client_fds_mutex_);
    active_client_fds_.insert(client_fd);
}

void SmtpServer::shutdown_active_clients() {
    const std::lock_guard lock(client_fds_mutex_);
    for (const int client_fd : active_client_fds_) {
        (void)::shutdown(client_fd, SHUT_RDWR);
    }
}

void SmtpServer::close_client_fd(int client_fd) {
    {
        const std::lock_guard lock(client_fds_mutex_);
        active_client_fds_.erase(client_fd);
    }
    (void)::shutdown(client_fd, SHUT_RDWR);
    close_fd(client_fd);
}

}  // namespace rapid_inbox::ingestd
