import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listJobs, pollJob, ApiError } from "./api.js";
import { listChatSessions, getChatSessionMessages } from "./api.js";
import { streamChatMessage } from "./stream.js";
import {
  groupThreads, latestJob, isProcessing, userMessageOf, replyOf, threadTitle, snippetOf, PROCESSING,
  mergeConversations, sessionTitle, sessionSnippet, sessionStatus, sessionProcessing
} from "./threads.js";
import {
  Bubble, StatusPill, ProgressSteps, ThreadListItem, Composer, Banner, EmptyState, IconButton,
  renderMarkdown
} from "./components.jsx";

const TERMINAL = new Set(["completed", "failed", "cancelled", "needs_review"]);
const NEW_CHAT = "new";

export function App() {
  const [jobs, setJobs] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [sessionMessages, setSessionMessages] = useState({}); // sessionId -> messages[]
  const [selected, setSelected] = useState(null); // null | NEW_CHAT | conversation item id
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [streamText, setStreamText] = useState(""); // accumulating reply for the active send
  const [liveLogs, setLiveLogs] = useState({}); // jobId -> logs[]
  const feedRef = useRef(null);
  const pollRef = useRef(0);

  const conversations = useMemo(() => mergeConversations(sessions, jobs), [sessions, jobs]);
  const activeItem = useMemo(
    () => (typeof selected === "string" && selected !== NEW_CHAT ? conversations.find(item => item.id === selected) : null),
    [conversations, selected]
  );
  const activeThread = activeItem?.type === "job" ? activeItem.thread : null;
  const activeSession = activeItem?.type === "session" ? activeItem.session : null;
  const activeMessages = activeSession ? sessionMessages[activeSession.id] || [] : [];

  const refreshJobs = useCallback(async () => {
    try {
      const data = await listJobs();
      setJobs(data.jobs || []);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const data = await listChatSessions();
      setSessions(data.sessions || []);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refreshJobs();
    refreshSessions();
  }, [refreshJobs, refreshSessions]);

  const refreshSessionMessages = useCallback(async sessionId => {
    try {
      const data = await getChatSessionMessages(sessionId);
      setSessionMessages(prev => ({ ...prev, [sessionId]: data.messages || [] }));
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    if (activeSession) refreshSessionMessages(activeSession.id);
  }, [activeSession?.id, refreshSessionMessages]);

  // Live polling for a legacy job thread (unchanged from Phase 1) and for an
  // escalated job_ref inside an active chat session.
  const activeJob = activeThread ? latestJob(activeThread) : null;
  const activeJobRef = activeMessages.length ? activeMessages[activeMessages.length - 1] : null;
  const pollingJobId = activeJob && PROCESSING.has(activeJob.status)
    ? activeJob.id
    : (activeJobRef?.kind === "job_ref" && PROCESSING.has(activeJobRef.job_status) ? activeJobRef.job_id : null);
  const awaitingSessionReply = Boolean(
    activeJobRef?.kind === "job_ref" && PROCESSING.has(activeJobRef.job_status) && !(activeJobRef.job_metadata || {}).final_response
  );

  useEffect(() => {
    if (!pollingJobId) return undefined;
    let stopped = false;
    let afterSequence = 0;
    let replyShown = false;
    const token = ++pollRef.current;

    const tick = async () => {
      if (stopped || pollRef.current !== token) return;
      try {
        const data = await pollJob(pollingJobId, afterSequence);
        if (stopped || pollRef.current !== token) return;
        const fresh = (data.new_logs || []).filter(log => Number(log.sequence) > afterSequence);
        if (fresh.length) {
          afterSequence = fresh[fresh.length - 1].sequence;
          setLiveLogs(prev => ({ ...prev, [pollingJobId]: [...(prev[pollingJobId] || []), ...fresh] }));
        }
        const status = data.job.status;
        const finalResponse = (data.job.metadata || {}).final_response;
        if (finalResponse && !replyShown) {
          replyShown = true;
          await refreshJobs();
          if (activeSession) await refreshSessionMessages(activeSession.id);
        }
        if (TERMINAL.has(status)) {
          await refreshJobs();
          if (activeSession) await refreshSessionMessages(activeSession.id);
          return;
        }
      } catch {
        /* transient poll failure — keep trying */
      }
      const delay = document.hidden ? 5000 : 1500;
      setTimeout(tick, delay);
    };

    tick();
    return () => { stopped = true; };
  }, [pollingJobId, refreshJobs, activeSession?.id, refreshSessionMessages]);

  const send = async text => {
    setSending(true);
    setStreamText("");
    setError("");
    try {
      const sessionId = activeSession ? activeSession.id : NEW_CHAT;
      let resolvedSessionId = activeSession ? activeSession.id : null;
      await streamChatMessage(sessionId, text, event => {
        if (event.type === "session") {
          resolvedSessionId = event.session_id;
          setSelected(`session:${resolvedSessionId}`);
        } else if (event.type === "delta") {
          setStreamText(prev => prev + event.text);
        } else if (event.type === "error") {
          setError(event.message || "The reply stream failed.");
        }
      });
      setDraft("");
      await refreshSessions();
      if (resolvedSessionId) await refreshSessionMessages(resolvedSessionId);
    } catch (err) {
      setError(err.message); // draft is preserved
    } finally {
      setStreamText("");
      setSending(false);
    }
  };

  // Auto-scroll the feed when content changes.
  const feedKey = activeThread
    ? `job:${activeThread.rootId}:${activeThread.jobs.length}:${(liveLogs[activeJob?.id] || []).length}`
    : activeSession
    ? `session:${activeSession.id}:${activeMessages.length}:${streamText.length}`
    : selected;
  useEffect(() => {
    const feed = feedRef.current;
    if (feed) feed.scrollTop = feed.scrollHeight;
  }, [feedKey]);

  const showConversation = selected !== null;
  const isLegacyReadOnly = Boolean(activeThread);
  const composerDisabled = sending || awaitingSessionReply;

  return (
    <div class={`chat-app ${showConversation ? "show-conversation" : ""}`}>
      <section class="thread-panel">
        <header class="panel-header">
          <h1>Chats</h1>
          <a class="admin-link" href="/admin">Admin</a>
          <IconButton label="New chat" onClick={() => { setSelected(NEW_CHAT); setDraft(""); setError(""); }}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path d="M10 3a1 1 0 0 1 1 1v5h5a1 1 0 1 1 0 2h-5v5a1 1 0 1 1-2 0v-5H4a1 1 0 1 1 0-2h5V4a1 1 0 0 1 1-1Z" />
            </svg>
          </IconButton>
        </header>
        <Banner message={!showConversation ? error : ""} onRetry={() => { refreshJobs(); refreshSessions(); }} />
        <div class="thread-list">
          {conversations.length === 0 && !error
            ? <EmptyState title="No conversations yet" hint="Start a new chat to talk to Arqis." />
            : conversations.map(item => (
                <ThreadListItem
                  key={item.id}
                  thread={item.type === "job" ? item.thread : { jobs: [{ created_at: item.session.updated_at || item.session.created_at }] }}
                  active={item.id === selected}
                  onSelect={() => setSelected(item.id)}
                  title={item.type === "job" ? threadTitle(item.thread) : sessionTitle(item.session)}
                  snippet={item.type === "job" ? snippetOf(item.thread) : sessionSnippet(item.session)}
                  processing={item.type === "job" ? isProcessing(item.thread) : sessionProcessing(item.session)}
                  status={item.type === "job" ? latestJob(item.thread).status : sessionStatus(item.session)}
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
          <h1>{activeThread ? threadTitle(activeThread) : activeSession ? sessionTitle(activeSession) : "New chat"}</h1>
          {activeJob && !replyOf(activeJob) ? <StatusPill status={activeJob.status} /> : null}
          {awaitingSessionReply ? <StatusPill status={activeJobRef.job_status} /> : null}
        </header>
        <Banner message={showConversation ? error : ""} onRetry={() => draft && send(draft)} />
        {showConversation ? (
          <div class="feed" ref={feedRef}>
            {!activeThread && !activeSession && (
              <EmptyState title="What can Arqis do for you?" hint="Casual messages get a quick reply; real work becomes a job." />
            )}
            {isLegacyReadOnly && (
              <p class="meta legacy-notice">This is a legacy conversation — read only.</p>
            )}
            {activeThread?.jobs.map(job => {
              const reply = replyOf(job);
              const logs = liveLogs[job.id] || [];
              const processing = PROCESSING.has(job.status) && !reply;
              return (
                <>
                  <Bubble role="user" text={userMessageOf(job)} time={job.created_at} />
                  <ProgressSteps logs={logs} running={processing} done={!processing && logs.length > 0} />
                  {reply
                    ? <Bubble role="assistant" html={renderMarkdown(reply)} time={job.completed_at} />
                    : null}
                  {!reply && job.status === "failed" && <Bubble role="system" text={job.last_error || "Job failed."} />}
                  {!reply && job.status === "cancelled" && <Bubble role="system" text="Job cancelled." />}
                  {!reply && job.status === "needs_review" && (
                    <Bubble role="system" text="Arqis needs a review decision — open Admin to approve." />
                  )}
                </>
              );
            })}
            {activeSession && activeMessages.map(message => {
              if (message.kind === "job_ref") {
                const reply = (message.job_metadata || {}).final_response;
                const logs = liveLogs[message.job_id] || [];
                const processing = PROCESSING.has(message.job_status) && !reply;
                return (
                  <>
                    <ProgressSteps logs={logs} running={processing} done={!processing && logs.length > 0} />
                    {reply
                      ? <Bubble role="assistant" html={renderMarkdown(reply)} time={message.created_at} />
                      : <Bubble role="assistant" text={message.content} time={message.created_at} />}
                    {!reply && message.job_status === "failed" && (
                      <Bubble role="system" text={message.job_last_error || "Job failed."} />
                    )}
                    {!reply && message.job_status === "needs_review" && (
                      <Bubble role="system" text="Arqis needs a review decision — open Admin to approve." />
                    )}
                  </>
                );
              }
              return (
                <Bubble
                  role={message.role}
                  html={message.role === "assistant" ? renderMarkdown(message.content) : undefined}
                  text={message.role === "user" ? message.content : undefined}
                  time={message.created_at}
                />
              );
            })}
            {sending && streamText && <Bubble role="assistant" text={streamText} streaming />}
            {sending && !streamText && <Bubble role="assistant" text="" streaming />}
          </div>
        ) : (
          <div class="feed" ref={feedRef}>
            <EmptyState title="Select a conversation" hint="Or start a new chat." />
          </div>
        )}
        {showConversation && !isLegacyReadOnly && (
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
