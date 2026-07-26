import { Component, type ErrorInfo, type ReactNode } from "react";
import { logClient } from "../clientLog";

/** Catches render-time crashes so they land in the client log (readable on a
 * phone via Logs -> Browser) and show a stack instead of a blank screen. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { err: Error | null }> {
  state = { err: null as Error | null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    logClient("error", "React render crash:", err.stack || err.message, info.componentStack || "");
  }

  render() {
    if (this.state.err) {
      return (
        <div style={{ padding: 16, overflow: "auto" }}>
          <h2 style={{ marginTop: 0 }}>Something crashed</h2>
          <pre className="error-text" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {this.state.err.stack || this.state.err.message}
          </pre>
          <button onClick={() => this.setState({ err: null })}>Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
