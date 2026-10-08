import test from "node:test";
import assert from "node:assert/strict";
import { parseSSE } from "../src/chat/stream.js";

function fakeBody(chunks) {
  let i = 0;
  return {
    getReader() {
      return {
        async read() {
          if (i >= chunks.length) return { done: true, value: undefined };
          const value = new TextEncoder().encode(chunks[i++]);
          return { done: false, value };
        }
      };
    }
  };
}

test("parses one event per data: line, split across reads", async () => {
  const body = fakeBody([
    'data: {"type":"delta","text":"He',
    'llo"}\n\n',
    'data: {"type":"done"}\n\n'
  ]);
  const events = [];
  for await (const event of parseSSE(body)) events.push(event);
  assert.deepEqual(events, [{ type: "delta", text: "Hello" }, { type: "done" }]);
});

test("ignores blank keep-alive chunks", async () => {
  const body = fakeBody(["\n\n", 'data: {"type":"delta","text":"hi"}\n\n']);
  const events = [];
  for await (const event of parseSSE(body)) events.push(event);
  assert.deepEqual(events, [{ type: "delta", text: "hi" }]);
});

test("multiple events in a single chunk are all parsed", async () => {
  const body = fakeBody(['data: {"type":"delta","text":"a"}\n\ndata: {"type":"delta","text":"b"}\n\n']);
  const events = [];
  for await (const event of parseSSE(body)) events.push(event);
  assert.deepEqual(events, [{ type: "delta", text: "a" }, { type: "delta", text: "b" }]);
});

test("salvages a final frame with no trailing blank line", async () => {
  const body = fakeBody(['data: {"type":"done"}']); // no trailing \n\n — connection closed right after
  const events = [];
  for await (const event of parseSSE(body)) events.push(event);
  assert.deepEqual(events, [{ type: "done" }]);
});

test("silently drops a truncated final frame with invalid JSON", async () => {
  const body = fakeBody(['data: {"type":"don']); // truncated mid-write, never valid JSON
  const events = [];
  for await (const event of parseSSE(body)) events.push(event);
  assert.deepEqual(events, []);
});
