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

}  // namespace
