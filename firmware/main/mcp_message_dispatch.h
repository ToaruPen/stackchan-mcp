#ifndef MCP_MESSAGE_DISPATCH_H
#define MCP_MESSAGE_DISPATCH_H

#include <string>
#include <utility>

namespace stackchan_mcp {

template <typename Schedule, typename SendNow>
void DispatchMcpMessage(
    bool caller_is_main_task,
    std::string payload,
    Schedule&& schedule,
    SendNow&& send_now) {
    if (caller_is_main_task) {
        std::forward<SendNow>(send_now)(payload);
        return;
    }

    auto send = std::forward<SendNow>(send_now);
    std::forward<Schedule>(schedule)(
        [payload = std::move(payload), send = std::move(send)]() mutable {
            send(payload);
        });
}

}  // namespace stackchan_mcp

#endif  // MCP_MESSAGE_DISPATCH_H
