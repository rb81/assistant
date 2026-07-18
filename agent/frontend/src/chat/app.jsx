import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listJobs, createJob, sendFollowUp, pollJob, ApiError } from "./api.js";
import {
  groupThreads, latestJob, isProcessing, userMessageOf, replyOf, threadTitle, snippetOf, PROCESSING
} from "./threads.js";
import {
  Bubble, StatusPill, ProgressSteps, ThreadListItem, Composer, Banner, EmptyState, IconButton,
  renderMarkdown
} from "./components.jsx";

const TERMINAL = new Set(["completed", "failed", "cancelled", "needs_review"]);
const NEW_CHAT = "new";

export function App() {
  const [jobs, setJobs] = useState([]);
  const [selected, setSelected] = useState(null); // null | NEW_CHAT | rootId (number)
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [liveLogs, setLiveLogs] = useState({}); // jobId -> logs[]
  const feedRef = useRef(null);
  const pollRef = useRef(0);

  const threads = useMemo(() => groupThreads(jobs), [jobs]);
  const activeThread = useMemo(
    () => (typeof selected === "number" ? threads.find(thread => thread.rootId === selected) : null),
    [threads, selected]
  );

  const refresh = useCallback(async () => {
    try {
      const data = await listJobs();
      setJobs(data.jobs || []);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Live polling of the newest processing job in the active thread.
  const activeJob = activeThread ? latestJob(activeThread) : null;
  const activeJobId = activeJob && PROCESSING.has(activeJob.status) ? activeJob.id : null;

  useEffect(() => {
    if (!activeJobId) return undefined;
    let stopped = false;
    let afterSequence = 0;
    const token = ++pollRef.current;

    const tick = async () => {
      if (stopped || pollRef.current !== token) return;
      try {
        const data = await pollJob(activeJobId, afterSequence);
        if (stopped || pollRef.current !== token) return;
        const fresh = (data.new_logs || []).filter(log => Number(log.sequence) > afterSequence);
        if (fresh.length) {
          afterSequence = fresh[fresh.length - 1].sequence;
          setLiveLogs(prev => ({
            ...prev,
            [activeJobId]: [...(prev[activeJobId] || []), ...fresh]
          }));
        }
        const status = data.job.status;
        const finalResponse = (data.job.metadata || {}).final_response;
        if (finalResponse || TERMINAL.has(status)) {
          await refresh();
          return;
        }
      } catch {
        /* transient poll failure — keep trying */
      }
      const delay = document.hidden ? 5000 : 1500;
      setTimeout(tick, delay);
    };

    tick();
    return () => {
      stopped = true;
    };
  }, [activeJobId, refresh]);

  const send = async text => {
    setSending(true);
    try {
      let data;
      if (activeThread) {
        data = await sendFollowUp(latestJob(activeThread).id, text);
      } else {
        data = await createJob(text);
      }
      setDraft("");
      setError("");
      await refresh();
      if (!activeThread && data?.job?.id) setSelected(data.job.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        await refresh(); // job still processing — state will show "working"
      } else {
        setError(err.message); // draft is preserved
      }
    } finally {
      setSending(false);
    }
  };

  // Auto-scroll the feed when content changes.
  const feedKey = activeThread
    ? `${activeThread.rootId}:${activeThread.jobs.length}:${(liveLogs[activeJob?.id] || []).length}`
    : selected;
  useEffect(() => {
    const feed = feedRef.current;
    if (feed) feed.scrollTop = feed.scrollHeight;
  }, [feedKey]);

  const showConversation = selected !== null;
  const composerDisabled = sending || Boolean(activeJobId);

  return (
    <div class={`chat-app ${showConversation ? "show-conversation" : ""}`}>
      <section class="thread-panel">
        <header class="panel-header">
          <h1>Chats</h1>
          <a class="admin-link" href="/admin">Admin</a>
          <IconButton label="New chat" onClick={() => { setSelected(NEW_CHAT); setDraft(""); }}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path d="M10 3a1 1 0 0 1 1 1v5h5a1 1 0 1 1 0 2h-5v5a1 1 0 1 1-2 0v-5H4a1 1 0 1 1 0-2h5V4a1 1 0 0 1 1-1Z" />
            </svg>
          </IconButton>
        </header>
        <Banner message={!showConversation ? error : ""} onRetry={refresh} />
        <div class="thread-list">
          {threads.length === 0 && !error
            ? <EmptyState title="No conversations yet" hint="Start a new chat to talk to Arqis." />
            : threads.map(thread => (
                <ThreadListItem
                  key={thread.rootId}
                  thread={thread}
                  active={thread.rootId === selected}
                  onSelect={() => setSelected(thread.rootId)}
                  title={threadTitle(thread)}
                  snippet={snippetOf(thread)}
                  processing={isProcessing(thread)}
                  status={latestJob(thread).status}
                />
              ))}
        </div>
      </section>

      <section class="conversation-panel">
        <header class="panel-header">
          <IconButton label="Back" extraClass="back-button" onClick={() => setSelected(null)}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path d="M12.7 4.3a1 1 0 0 1 0 1.4L8.4 10l4.3 4.3a1 1 0 1 1-1.4 1.4l-5-5a1 1 0 0 1 0-1.4l5-5a1 1 0 0 1 1.4 0Z" />
            </svg>
          </IconButton>
          <h1>{activeThread ? threadTitle(activeThread) : "New chat"}</h1>
          {activeJob ? <StatusPill status={activeJob.status} /> : null}
        </header>
        <Banner message={showConversation ? error : ""} onRetry={refresh} />
        {showConversation ? (
          <div class="feed" ref={feedRef}>
            {!activeThread && (
              <EmptyState
                title="What can Arqis do for you?"
                hint="Messages become jobs — you can also continue over email."
              />
            )}
            {activeThread?.jobs.map(job => {
              const reply = replyOf(job);
              const logs = liveLogs[job.id] || [];
              const processing = PROCESSING.has(job.status);
              return (
                <>
                  <Bubble role="user" text={userMessageOf(job)} time={job.created_at} />
                  <ProgressSteps logs={logs} running={processing} done={!processing && logs.length > 0} />
                  {reply
                    ? <Bubble role="assistant" html={renderMarkdown(reply)} time={job.completed_at} />
                    : null}
                  {!reply && job.status === "failed" && (
                    <Bubble role="system" text={job.last_error || "Job failed."} />
                  )}
                  {!reply && job.status === "cancelled" && <Bubble role="system" text="Job cancelled." />}
                  {!reply && job.status === "needs_review" && (
                    <Bubble role="system" text="Arqis needs a review decision — open Admin to approve." />
                  )}
                </>
              );
            })}
          </div>
        ) : (
          <div class="feed" ref={feedRef}>
            <EmptyState title="Select a conversation" hint="Or start a new chat." />
          </div>
        )}
        {showConversation && (
          <Composer
            disabled={composerDisabled}
            busyLabel="Arqis is working…"
            onSend={send}
            draft={draft}
            onDraft={setDraft}
          />
        )}
      </section>
    </div>
  );
}
