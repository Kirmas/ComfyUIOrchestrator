import { useEffect, useRef } from "react";
import { getApiToken, getWsBaseUrl } from "./client";
import type { ProgressEvent } from "../types";

export function useProjectWs(projectId: string | null, onEvent: (event: ProgressEvent) => void) {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!projectId) return;

    let closedByUs = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const url = `${getWsBaseUrl()}/ws/projects/${projectId}?token=${encodeURIComponent(getApiToken())}`;
      socket = new WebSocket(url);
      socket.onmessage = (evt) => {
        try {
          onEventRef.current(JSON.parse(evt.data) as ProgressEvent);
        } catch {
          // ignore malformed frames
        }
      };
      socket.onclose = () => {
        if (!closedByUs) retryTimer = setTimeout(connect, 2000);
      };
    };

    connect();

    return () => {
      closedByUs = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [projectId]);
}
