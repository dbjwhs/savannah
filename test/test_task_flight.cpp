// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Integration: the task supervisor end to end, token-free. Spawns
// savannahd --tcp 0 against a session-aware fake-claude and a scratch git
// repo, then drives the full lifecycle over TCP:
//   task_new (with worktree) -> turn 1 runs -> tail it
//   task_send -> turn 2 resumes the SAME session (counter carries)
//   a second task is isolated (own worktree, own session id)
//   cancel one -> the other still drives
//
// Needs cwd = build dir (ctest sets WORKING_DIRECTORY) with ./savannahd,
// ./fake-claude, cfg_task.toml. Skips (77) if git is unavailable.

#include <song/song.hpp>

#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

#include "savannah.hpp"
#include "test_support.hpp"

namespace sw = song::savannah;
namespace st = savannah::test;
using savannah::ChunkKind;
using song::ServiceConnection;

namespace {

// Every call below goes through the generated proxy; these only fill in the
// arguments the test never varies.
sw::TaskInfo task_new(ServiceConnection& conn, const std::string& title,
                      const std::string& prompt, bool worktree) {
    sw::TaskSpec spec;
    spec.title = title;
    spec.prompt = prompt;
    spec.workdir = "";
    spec.make_worktree = worktree;
    spec.timeout_ms = 0;
    return sw::AgentNodeProxy(conn).task_new(spec);
}

sw::TaskInfo task_status(ServiceConnection& conn, const std::string& id) {
    return sw::AgentNodeProxy(conn).task_status(id);
}

bool task_send(ServiceConnection& conn, const std::string& id,
               const std::string& prompt) {
    return sw::AgentNodeProxy(conn).task_send(id, prompt);
}

bool task_cancel(ServiceConnection& conn, const std::string& id) {
    return sw::AgentNodeProxy(conn).task_cancel(id);
}

bool task_rm(ServiceConnection& conn, const std::string& id) {
    return sw::AgentNodeProxy(conn).task_rm(id);
}

std::size_t task_count(ServiceConnection& conn) {
    return sw::AgentNodeProxy(conn).task_list().tasks.size();
}

// Tail a task; concatenate its TEXT chunks.
std::string task_output(ServiceConnection& conn, const std::string& id) {
    std::string text;
    sw::AgentNodeProxy(conn).task_output(
        id,
        [&](sw::AgentChunk& c) {
            if (st::is_kind(c, ChunkKind::Text)) text += c.payload;
        },
        /*chunk_timeout_ms=*/30000);
    return text;
}

// Poll task status until state == want (or budget elapses).
sw::TaskInfo wait_state(ServiceConnection& conn, const std::string& id,
                        const std::string& want, int budget_ms = 10000) {
    sw::TaskInfo info;
    for (int waited = 0; waited < budget_ms; waited += 20) {
        info = task_status(conn, id);
        if (info.state == want) return info;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return info;
}

}  // namespace

int main() {
    if (std::system("git --version >/dev/null 2>&1") != 0) {
        std::printf("test_task_flight: skipped (git not available)\n");
        return 77;
    }

    // fake-claude's session counter lives under $TMPDIR; point it at this
    // build dir (guaranteed writable) so the count survives across turns
    // regardless of the worker's worktree cwd.
    char cwd[4096];
    if (getcwd(cwd, sizeof cwd)) setenv("TMPDIR", cwd, 1);
    std::system("rm -f fake-claude-session-*.count");

    // Scratch git repo the worktrees branch from.
    std::system(
        "rm -rf task_repo && git init -q task_repo && "
        "git -C task_repo config user.email t@t && "
        "git -C task_repo config user.name t && "
        "git -C task_repo commit -q --allow-empty -m init");

    auto d = st::Daemon::spawn("cfg_task.toml");
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (st::g_failures) return 1;

    auto conn = st::connect_tcp("127.0.0.1", d.port);

    // ---- task_new with a worktree drives turn 1 ----
    auto t1 = task_new(conn, "adder", "add a feature", /*worktree=*/true);
    CHECK(!t1.id.empty());
    CHECK(!t1.session_id.empty());
    CHECK(t1.worktree.find(".savannah-worktrees") != std::string::npos);

    auto s1 = wait_state(conn, t1.id, "idle");
    CHECK(s1.state == "idle");
    CHECK(s1.turns == 1);
    CHECK(s1.last_line.find("turn 1") != std::string::npos);

    // ---- tail replays the transcript ----
    CHECK(task_output(conn, t1.id).find("turn 1") != std::string::npos);

    // ---- task_send resumes the SAME session: counter carries to turn 2 ----
    CHECK(task_send(conn, t1.id, "keep going") == true);
    auto s2 = wait_state(conn, t1.id, "idle");
    CHECK(s2.turns == 2);
    CHECK(s2.session_id == t1.session_id);  // stable session id
    CHECK(s2.last_line.find("turn 2") != std::string::npos);  // context carried

    // ---- a second task is isolated ----
    auto t2 = task_new(conn, "docs", "update the readme", /*worktree=*/true);
    wait_state(conn, t2.id, "idle");
    CHECK(t2.id != t1.id);
    CHECK(t2.session_id != t1.session_id);
    CHECK(t2.worktree != t1.worktree);
    CHECK(task_count(conn) == 2);

    // ---- cancel t1; t2 still drives ----
    CHECK(task_cancel(conn, t1.id) == true);
    CHECK(task_status(conn, t1.id).state == "cancelled");
    CHECK(task_send(conn, t2.id, "one more") == true);
    auto s2b = wait_state(conn, t2.id, "idle");
    CHECK(s2b.turns == 2);

    // ---- a cancelled task rejects further sends; unknown ids too ----
    CHECK(task_send(conn, t1.id, "nope") == false);
    CHECK(task_send(conn, "t-9999", "nope") == false);

    // ---- session handoff: DECISION_NEEDED survives verbatim to last_line ----
    // The handoff pattern (docs/handoff.md) has no wire surface of its own: a
    // worker asks for a human by ending its turn with a marker line, and every
    // consumer (dash inbox, conductor Claude, CI wrapper) reads it back out of
    // last_line via task_list/task_status. This pins that contract: the final
    // line of the final TEXT block reaches last_line byte for byte (no
    // truncation, no flattening), and the answer turn replaces it.
    const std::string decision =
        "DECISION_NEEDED {\"question\":\"ship it?\",\"options\":[\"yes\",\"no\"]}";
    auto t4 = task_new(conn, "handoff", "checks done\n" + decision,
                       /*worktree=*/false);
    auto s4 = wait_state(conn, t4.id, "idle");
    CHECK(s4.state == "idle");
    CHECK(s4.last_line.size() >= decision.size() &&
          s4.last_line.compare(s4.last_line.size() - decision.size(),
                               decision.size(), decision) == 0);
    CHECK(task_send(conn, t4.id, "yes, ship it") == true);
    auto s5 = wait_state(conn, t4.id, "idle");
    CHECK(s5.turns == 2);
    CHECK(s5.last_line.find("DECISION_NEEDED") == std::string::npos);

    // ---- auto-continue across the per-invocation max-turns cap ----
    // A MAXTURNS-marked prompt makes fake-claude emit subtype error_max_turns
    // on its first invocation. The supervisor must auto-resume ("Continue...")
    // WITHOUT a second task_send and land on idle, not stall. Two invocations
    // (the capped one + the auto-continue) advance turns by 2 from one
    // task_new; the old behavior stopped at turns == 1.
    auto t3 = task_new(conn, "long", "MAXTURNS do a big job", /*worktree=*/true);
    auto s3 = wait_state(conn, t3.id, "idle");
    CHECK(s3.state == "idle");   // finished on its own, not stalled/"incomplete"
    CHECK(s3.turns == 2);        // capped invocation + one auto-continue
    // The auto-continue's second invocation is delimited in the transcript so
    // the tail does not run the turns together (readability).
    CHECK(task_output(conn, t3.id).find("----- turn 2 -----") != std::string::npos);

    // ---- task_rm forgets finished tasks; the table shrinks ----
    // t1 is cancelled, t4 (handoff) is idle: both removable. A removed id is
    // truly gone (status empty, second rm false); unknown ids refuse.
    std::size_t before = task_count(conn);
    CHECK(task_rm(conn, t1.id) == true);
    CHECK(task_status(conn, t1.id).id.empty());
    CHECK(task_rm(conn, t1.id) == false);
    CHECK(task_rm(conn, "t-9999") == false);
    CHECK(task_rm(conn, t4.id) == true);
    CHECK(task_count(conn) == before - 2);
    // A tail of a removed task reports the error result, same as unknown.
    CHECK(task_output(conn, t1.id).find("hello") == std::string::npos);

    return st::finish("test_task_flight");
}
