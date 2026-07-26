import "@google/model-viewer";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { installClientLogging } from "./clientLog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";

installClientLogging();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
