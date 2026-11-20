import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { LiveDeskProvider } from "./live/LiveDeskContext";
import "./styles/tokens.css";
import "./styles/terminal.css";
import "./styles/panels.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LiveDeskProvider>
      <App />
    </LiveDeskProvider>
  </StrictMode>,
);
