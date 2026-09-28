import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const rootEl = document.getElementById("root");
if (!rootEl) {
  // index.html ships a #root div; if it's missing the deploy is broken
  // and there's nothing useful to render. Fail loud rather than silent.
  throw new Error("missing #root element in index.html");
}

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
