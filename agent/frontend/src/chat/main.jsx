import { render } from "preact";
import { App } from "./app.jsx";
import "./chat.css";

render(<App />, document.getElementById("chat-root"));

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
