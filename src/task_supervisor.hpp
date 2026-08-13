// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// task_supervisor.hpp — the persistent-worker half of a savannah node.
//
// A "task" is a long-lived, resumable claude session bound to a working
// directory (a git worktree for coding tasks). Unlike ask() (stateless,
// single-shot), a task is driven turn by turn: task_new creates it,
// task_send drives another turn asynchronously, task_output tails the
// transcript, task_cancel kills any running turn and prunes the worktree.
//
// Authoritative state lives here, in savannahd's own long-lived table —
// deliberately NOT in song objects (which die with the creating connection;
// see the object-system findings). song classes/properties are layered on
// later as a live-status push channel; this table stays the source of truth.
//
// Threading: a supervisor-level mutex guards the task map; each task holds
// its own mutex for transcript/state and runs at most one turn at a time
// (per-worker single-flight) on a background thread.

#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "config.hpp"
#include "stream_json.hpp"

namespace savannah {

/// What the caller asked us to create.
struct TaskCreate {
    std::string title;
    std::string prompt;         // turn 1; empty = create idle
    std::string workdir;        // base dir; empty = node workspace
    bool make_worktree = false; // git worktree add for isolation
    std::int64_t timeout_ms = 0;// per-turn; 0 = node default
};

/// A read-only snapshot of one task (song-agnostic; savannahd maps to wire).
struct TaskView {
    std::string id;
    std::string title;
    std::string state;       // running | idle | incomplete | failed | cancelled
                             // incomplete = hit the auto-continue budget with
                             // work still pending; send again to keep going
    std::string worktree;    // cwd the worker runs in
    std::string session_id;  // claude session uuid
    std::uint32_t turns = 0;
    std::string last_line;
};

class TaskSupervisor {
public:
    explicit TaskSupervisor(const NodeConfig& config) : config_(config) {}
    ~TaskSupervisor();

    TaskSupervisor(const TaskSupervisor&) = delete;
    TaskSupervisor& operator=(const TaskSupervisor&) = delete;

    /// Create a task; drives turn 1 in the background if prompt is non-empty.
    TaskView create(const TaskCreate& spec);

    /// Snapshot every task.
    std::vector<TaskView> list();

    /// Snapshot one task; view.id empty if unknown.
    TaskView status(const std::string& id);

    /// Drive one more turn (async). False if unknown or a turn is running.
    bool send(const std::string& id, const std::string& prompt);

    /// Tail a task: replays its transcript, then follows the live turn until
    /// it ends. `emit` is called per chunk. Unknown id emits one error RESULT.
    void tail(const std::string& id, const std::function<void(const Chunk&)>& emit);

    /// Kill any running turn, prune the worktree. True if the task existed.
    bool cancel(const std::string& id);

private:
    struct Task {
        std::string id;
        std::string title;
        std::string workdir;      // base repo dir
        std::string worktree;     // cwd (worktree path, or workdir)
        std::string branch;       // worktree branch, "" if none
        std::string session_id;
        std::int64_t timeout_ms = 0;

        std::mutex m;             // guards state/transcript/last_line/turns
        std::string state = "idle";
        std::uint32_t turns = 0;
        std::string last_line;
        std::vector<Chunk> transcript;

        std::shared_ptr<std::atomic<bool>> cancel =
            std::make_shared<std::atomic<bool>>(false);
        std::thread worker;       // the active/last turn
    };

    void drive_turn(Task* t, const std::string& prompt);
    TaskView snapshot(Task& t);  // caller holds t.m

    const NodeConfig& config_;
    std::mutex mu_;                                    // guards tasks_ + next_
    std::map<std::string, std::unique_ptr<Task>> tasks_;
    std::uint64_t next_ = 1;
};

}  // namespace savannah
