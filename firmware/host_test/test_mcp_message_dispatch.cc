#include <gtest/gtest.h>

#include <functional>
#include <string>
#include <utility>
#include <vector>

#include "mcp_message_dispatch.h"

namespace {

class FakeMainLoop {
public:
    void Schedule(std::function<void()> task) {
        queued_tasks_.push_back(std::move(task));
    }

    void RunOneTurn() {
        auto tasks = std::move(queued_tasks_);
        queued_tasks_.clear();
        on_main_task_ = true;
        for (auto& task : tasks) {
            task();
        }
        on_main_task_ = false;
    }

    bool on_main_task() const {
        return on_main_task_;
    }

    size_t queued_task_count() const {
        return queued_tasks_.size();
    }

private:
    bool on_main_task_ = false;
    std::vector<std::function<void()>> queued_tasks_;
};

TEST(McpMessageDispatchTest, ToolReplyIsSentInTheSameScheduledMainTurn) {
    FakeMainLoop loop;
    std::vector<std::string> sent;

    for (const std::string payload : {"tool-result", "tool-error"}) {
        const size_t sent_before_dispatch = sent.size();
        loop.Schedule([&, payload]() {
            stackchan_mcp::DispatchMcpMessage(
                loop.on_main_task(),
                payload,
                [&](std::function<void()> task) {
                    loop.Schedule(std::move(task));
                },
                [&](const std::string& sent_payload) {
                    sent.push_back(sent_payload);
                });
        });

        loop.RunOneTurn();

        ASSERT_EQ(sent.size(), sent_before_dispatch + 1);
        EXPECT_EQ(sent.back(), payload);
        EXPECT_EQ(loop.queued_task_count(), 0U);
    }

    EXPECT_EQ(sent, std::vector<std::string>({"tool-result", "tool-error"}));
}

TEST(McpMessageDispatchTest, WebsocketReplyStillWaitsForTheMainTask) {
    FakeMainLoop loop;
    std::vector<std::string> sent;

    for (const std::string payload : {
             "initialize-result",
             "validation-error",
             "tools-list-result"}) {
        const size_t sent_before_dispatch = sent.size();
        stackchan_mcp::DispatchMcpMessage(
            loop.on_main_task(),
            payload,
            [&](std::function<void()> task) {
                loop.Schedule(std::move(task));
            },
            [&](const std::string& sent_payload) {
                sent.push_back(sent_payload);
            });

        EXPECT_EQ(sent.size(), sent_before_dispatch);
        ASSERT_EQ(loop.queued_task_count(), 1U);

        loop.RunOneTurn();
        ASSERT_EQ(sent.size(), sent_before_dispatch + 1);
        EXPECT_EQ(sent.back(), payload);
        EXPECT_EQ(loop.queued_task_count(), 0U);
    }

    EXPECT_EQ(
        sent,
        std::vector<std::string>({
            "initialize-result",
            "validation-error",
            "tools-list-result"}));
}

TEST(McpMessageDispatchTest, ToolResultCarriesBoundedSameClockStageTiming) {
    const auto timing = stackchan_mcp::BuildMcpStageTiming(
        100,
        200,
        220,
        227,
        1
    );

    EXPECT_EQ(timing.receive_to_apply_us, 100U);
    EXPECT_EQ(timing.tool_apply_us, 20U);
    EXPECT_EQ(timing.apply_to_reply_enqueue_us, 7U);
    EXPECT_EQ(timing.scheduler_hops, 1U);
    EXPECT_EQ(
        stackchan_mcp::AddMcpStageTiming(
            R"({"content":[],"isError":false})",
            timing
        ),
        R"({"content":[],"isError":false,"mcpStageUs":{"receiveToApply":100,"toolApply":20,"applyToReplyEnqueue":7,"schedulerHops":1}})"
    );
}

TEST(McpMessageDispatchTest, StageTimingClampsNonMonotonicClockEdgesToZero) {
    const auto timing = stackchan_mcp::BuildMcpStageTiming(
        200,
        100,
        90,
        80,
        1
    );

    EXPECT_EQ(timing.receive_to_apply_us, 0U);
    EXPECT_EQ(timing.tool_apply_us, 0U);
    EXPECT_EQ(timing.apply_to_reply_enqueue_us, 0U);
}

TEST(McpMessageDispatchTest, TimedToolReplySamplesAfterPayloadPreparation) {
    std::vector<std::string> events;
    std::string sent;

    stackchan_mcp::DispatchTimedMcpToolResult(
        7,
        R"({"content":[],"isError":false})",
        100,
        200,
        220,
        [&events]() {
            events.push_back("clock");
            return 227U;
        },
        [&events, &sent](std::string payload) {
            events.push_back("send");
            sent = std::move(payload);
        }
    );

    EXPECT_EQ(events, std::vector<std::string>({"clock", "send"}));
    EXPECT_EQ(
        sent,
        R"({"jsonrpc":"2.0","id":7,"result":{"content":[],"isError":false,"mcpStageUs":{"receiveToApply":100,"toolApply":20,"applyToReplyEnqueue":7,"schedulerHops":1}}})"
    );
}

}  // namespace
