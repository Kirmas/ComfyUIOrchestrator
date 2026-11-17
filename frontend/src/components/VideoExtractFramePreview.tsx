import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";

export interface VideoExtractFramePreviewProps {
  videoUrl: string | null;
  frameIndex: number;
  onCommit: (frameIndex: number) => void;
  fps?: number;
}

export function VideoExtractFramePreview({
  videoUrl,
  frameIndex,
  onCommit,
  fps = 24,
}: VideoExtractFramePreviewProps) {
  const t = useT();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [customFps, setCustomFps] = useState<number>(fps && fps > 0 ? fps : 24);

  const currentFrame = Math.floor(currentTime * customFps + 0.0001);
  const totalFrames = duration > 0 ? Math.floor(duration * customFps + 0.0001) : null;

  useEffect(() => {
    if (fps && fps > 0) {
      setCustomFps(fps);
    }
  }, [fps]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handleLoadedMetadata = () => {
      setDuration(video.duration || 0);
      setCurrentTime(video.currentTime || 0);
    };

    const handleTimeUpdate = () => {
      setCurrentTime(video.currentTime);
    };

    video.addEventListener("loadedmetadata", handleLoadedMetadata);
    video.addEventListener("timeupdate", handleTimeUpdate);

    return () => {
      video.removeEventListener("loadedmetadata", handleLoadedMetadata);
      video.removeEventListener("timeupdate", handleTimeUpdate);
    };
  }, []);

  const stepFrames = (delta: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.pause();
    const targetFrame = Math.max(0, currentFrame + delta);
    const newTime = Math.max(0, Math.min(video.duration || 0, (targetFrame + 0.5) / customFps));
    video.currentTime = newTime;
    setCurrentTime(newTime);
  };

  const handlePickCurrent = () => {
    onCommit(currentFrame);
  };

  const handleJumpToPicked = () => {
    const video = videoRef.current;
    if (!video) return;
    video.pause();
    const targetTime = (frameIndex + 0.5) / customFps;
    video.currentTime = Math.min(video.duration || 0, Math.max(0, targetTime));
    setCurrentTime(video.currentTime);
  };

  if (!videoUrl) {
    return (
      <div
        style={{
          padding: "16px",
          background: "var(--bg-secondary)",
          border: "1px dashed var(--border)",
          borderRadius: "8px",
          textAlign: "center",
          fontSize: "12px",
          color: "var(--text-dim)",
          marginBottom: "10px",
        }}
      >
        {t("videoFrame.noSource")}
      </div>
    );
  }

  const pickedTimeSec = (frameIndex / customFps).toFixed(2);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        background: "var(--bg-secondary)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
        padding: "12px",
        marginBottom: "10px",
      }}
    >
      <video
        ref={videoRef}
        src={videoUrl}
        controls
        playsInline
        preload="metadata"
        style={{
          width: "100%",
          maxHeight: "320px",
          borderRadius: "6px",
          backgroundColor: "#000",
        }}
      />

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
          fontSize: "12px",
        }}
      >
        <span style={{ fontWeight: 500 }}>
          {t("videoFrame.current", { frame: currentFrame, time: currentTime.toFixed(2) })}
          {totalFrames !== null && (
            <span style={{ color: "var(--text-dim)", marginLeft: "4px" }}>
              {t("videoFrame.total", { total: totalFrames })}
            </span>
          )}
        </span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          <span style={{ fontSize: "11px", color: "var(--text-dim)" }}>FPS:</span>
          <select
            value={customFps}
            onChange={(e) => setCustomFps(Number(e.target.value))}
            style={{ fontSize: "11px", padding: "2px 4px", borderRadius: "4px" }}
          >
            {![12, 16, 24, 25, 30, 50, 60].includes(customFps) && (
              <option value={customFps}>{customFps} (auto)</option>
            )}
            <option value={12}>12</option>
            <option value={16}>16</option>
            <option value={24}>24{fps === 24 ? " (auto)" : ""}</option>
            <option value={25}>25{fps === 25 ? " (auto)" : ""}</option>
            <option value={30}>30{fps === 30 ? " (auto)" : ""}</option>
            <option value={50}>50{fps === 50 ? " (auto)" : ""}</option>
            <option value={60}>60{fps === 60 ? " (auto)" : ""}</option>
          </select>
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px" }}>
        <button
          type="button"
          onClick={() => stepFrames(-5)}
          style={{ fontSize: "11px", padding: "3px 7px" }}
          title="-5 frames"
        >
          -5
        </button>
        <button
          type="button"
          onClick={() => stepFrames(-1)}
          style={{ fontSize: "11px", padding: "3px 7px" }}
          title="-1 frame"
        >
          -1
        </button>
        <button
          type="button"
          onClick={() => stepFrames(1)}
          style={{ fontSize: "11px", padding: "3px 7px" }}
          title="+1 frame"
        >
          +1
        </button>
        <button
          type="button"
          onClick={() => stepFrames(5)}
          style={{ fontSize: "11px", padding: "3px 7px" }}
          title="+5 frames"
        >
          +5
        </button>

        <div style={{ width: "1px", height: "18px", background: "var(--border)", margin: "0 4px" }} />

        <button
          type="button"
          onClick={handlePickCurrent}
          style={{
            fontSize: "11px",
            padding: "3px 10px",
            background: "var(--accent)",
            color: "#fff",
            border: "none",
            borderRadius: "4px",
            fontWeight: 500,
          }}
        >
          {t("videoFrame.pickCurrent", { frame: currentFrame })}
        </button>

        <button
          type="button"
          onClick={handleJumpToPicked}
          style={{ fontSize: "11px", padding: "3px 8px", background: "var(--panel)", marginLeft: "auto" }}
        >
          🔍 {t("videoFrame.jumpToPicked")}
        </button>
      </div>

      <div
        style={{
          fontSize: "11px",
          padding: "6px 10px",
          borderRadius: "4px",
          background: "var(--panel)",
          border: "1px solid var(--border)",
          color: "var(--text)",
        }}
      >
        {t("videoFrame.picked", { frame: frameIndex, time: pickedTimeSec })}
      </div>
    </div>
  );
}
