#ifndef MCP_MESSAGE_DISPATCH_H
#define MCP_MESSAGE_DISPATCH_H

#include <cstdint>
#include <limits>
#include <string>
#include <utility>

namespace stackchan_mcp {

struct McpStageTiming {
    uint64_t receive_to_apply_us = 0;
    uint64_t tool_apply_us = 0;
    uint64_t apply_to_reply_enqueue_us = 0;
    uint32_t scheduler_hops = 0;
};

inline uint64_t NonnegativeClockDelta(uint64_t start, uint64_t end) {
    return end >= start ? end - start : 0;
}

inline McpStageTiming BuildMcpStageTiming(
    uint64_t received_at_us,
    uint64_t apply_started_at_us,
    uint64_t apply_finished_at_us,
    uint64_t reply_enqueued_at_us,
    uint32_t scheduler_hops
) {
    return McpStageTiming{
        .receive_to_apply_us = NonnegativeClockDelta(
            received_at_us,
            apply_started_at_us
        ),
        .tool_apply_us = NonnegativeClockDelta(
            apply_started_at_us,
            apply_finished_at_us
        ),
        .apply_to_reply_enqueue_us = NonnegativeClockDelta(
            apply_finished_at_us,
            reply_enqueued_at_us
        ),
        .scheduler_hops = scheduler_hops,
    };
}

inline std::string AddMcpStageTiming(
    std::string result,
    const McpStageTiming& timing
) {
    if (result.size() < 2 || result.front() != '{' || result.back() != '}') {
        return result;
    }
    result.pop_back();
    if (result.size() > 1) {
        result += ',';
    }
    result += "\"mcpStageUs\":{\"receiveToApply\":";
    result += std::to_string(timing.receive_to_apply_us);
    result += ",\"toolApply\":";
    result += std::to_string(timing.tool_apply_us);
    result += ",\"applyToReplyEnqueue\":";
    result += std::to_string(timing.apply_to_reply_enqueue_us);
    result += ",\"schedulerHops\":";
    result += std::to_string(timing.scheduler_hops);
    result += "}}";
    return result;
}

template <typename Clock, typename Send>
bool DispatchTimedMcpToolResult(
    int id,
    const std::string& result,
    uint64_t received_at_us,
    uint64_t apply_started_at_us,
    uint64_t apply_finished_at_us,
    Clock&& clock,
    Send&& send
) {
    const auto placeholder_timing = McpStageTiming{
        .receive_to_apply_us = NonnegativeClockDelta(
            received_at_us,
            apply_started_at_us
        ),
        .tool_apply_us = NonnegativeClockDelta(
            apply_started_at_us,
            apply_finished_at_us
        ),
        .apply_to_reply_enqueue_us = std::numeric_limits<uint64_t>::max(),
        .scheduler_hops = 1,
    };
    const std::string prepared_result = AddMcpStageTiming(
        result,
        placeholder_timing
    );
    const std::string marker =
        "\"applyToReplyEnqueue\":" +
        std::to_string(std::numeric_limits<uint64_t>::max());
    const auto marker_offset = prepared_result.rfind(marker);
    if (marker_offset == std::string::npos) {
        return false;
    }

    std::string payload = "{\"jsonrpc\":\"2.0\",\"id\":";
    payload += std::to_string(id) + ",\"result\":";
    payload += prepared_result;
    payload += "}";

    const uint64_t reply_enqueued_at_us =
        static_cast<uint64_t>(std::forward<Clock>(clock)());
    const auto timing = BuildMcpStageTiming(
        received_at_us,
        apply_started_at_us,
        apply_finished_at_us,
        reply_enqueued_at_us,
        1
    );
    const auto payload_marker_offset = payload.rfind(marker);
    if (payload_marker_offset == std::string::npos) {
        return false;
    }
    payload.replace(
        payload_marker_offset + marker.find(':') + 1,
        std::to_string(std::numeric_limits<uint64_t>::max()).size(),
        std::to_string(timing.apply_to_reply_enqueue_us)
    );
    std::forward<Send>(send)(std::move(payload));
    return true;
}

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
