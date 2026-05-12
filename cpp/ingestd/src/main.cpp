#include "config.h"
#include "ingest_app.h"
#include "smtp_server.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <exception>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

namespace {

std::atomic<bool> stop_requested{false};

void request_stop(int) {
    stop_requested.store(true);
}

}

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--help") {
        std::cout << "usage: rapid-inbox-ingestd [--base-dir PATH] [--writer-smoke]\n";
        return 0;
    }

    std::filesystem::path base_dir = std::filesystem::current_path();
    bool writer_smoke = false;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--base-dir" && i + 1 < argc) {
            base_dir = argv[++i];
        } else if (arg == "--writer-smoke") {
            writer_smoke = true;
        }
    }

    try {
        stop_requested.store(false);
        std::signal(SIGTERM, request_stop);
        std::signal(SIGINT, request_stop);

        auto config = rapid_inbox::ingestd::Config::load(base_dir);
        rapid_inbox::ingestd::IngestApp app(config);
        app.start_writer();
        if (writer_smoke) {
            app.stop_and_drain();
            std::cout << "writer smoke ok\n";
            return 0;
        }
        rapid_inbox::ingestd::SmtpServer server(config.smtp_host,
                                                config.smtp_port,
                                                app.domains(),
                                                app.queue(),
                                                config.max_recipients_per_message,
                                                config.max_message_size_bytes);
        server.start();
        std::cout << "rapid-inbox-ingestd listening on " << config.smtp_host << ":"
                  << config.smtp_port << "\n";
        while (!stop_requested.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        server.stop();
        app.stop_and_drain();
        std::cout << "rapid-inbox-ingestd stopped after drain\n";
    } catch (const std::exception& exc) {
        std::cerr << "rapid-inbox-ingestd failed: " << exc.what() << "\n";
        return 1;
    }
}
