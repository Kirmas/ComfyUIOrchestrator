import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";

export interface VideoTrimPreviewProps {
  videoUrl: string | null;
  startFrame: number;
  endFrame: number;
  onCommit: (startFrame: number, endFrame: number) => void;
  fps?: number;
}

export function VideoTrimPreview({
  videoUrl,
  startFrame,
  endFrame,
  onCommit,
  fps = 30,
}: VideoTrimPreviewProps) {
  const t = useT();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [isPlayingRange, setIsPlayingRange] = useState<boolean>(false);
  const [customFps, setCustomFps] = useState<number>(fps);

  const currentFrame = Math.floor(currentTime * customFps);
  const totalFrames = duration > 0 ? Math.floor(duration * customFps) : null;

  useEffect(() => {
    setCustomFps(fps);
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
      if (isPlayingRange) {
        const stopTime = endFrame > 0 ? (endFrame + 1) / customFps : video.duration;
        if (video.currentTime >= stopTime) {
          video.pause();
          setIsPlayingRange(false);
        }
      }
    };

    const handlePause = () => {
      setIsPlayingRange(false);
    };

    video.addEventListener("loadedmetadata", handleLoadedMetadata);
    video.addEventListener("timeupdate", handleTimeUpdate);
    video.addEventListener("pause", handlePause);

    return () => {
      video.removeEventListener("loadedmetadata", handleLoadedMetadata);
      video.removeEventListener("timeupdate", handleTimeUpdate);
      video.removeEventListener("pause", handlePause);
    };
  }, [isPlayingRange, endFrame, customFps]);

  const stepFrames = (delta: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.pause();
    setIsPlayingRange(false);
    const newTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + delta / customFps));
    video.currentTime = newTime;
    setCurrentTime(newTime);
  };

  const handleSetStart = () => {
    onCommit(currentFrame, endFrame);
  };

  const handleSetEnd = () => {
    onCommit(startFrame, currentFrame);
  };

  const handleResetEnd = () => {
    onCommit(startFrame, 0);
  };

  const handlePlayRange = () => {
    const video = videoRef.current;
    if (!video) return;

    if (isPlayingRange) {
      video.pause();
      setIsPlayingRange(false);
      return;
    }

    const startTime = startFrame / customFps;
    video.currentTime = startTime;
    setIsPlayingRange(true);
    video.play().catch(() => setIsPlayingRange(false));
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
        {t("videoTrim.noSource")}
      </div>
    );
  }

  const frameCount = endFrame > 0 ? Math.max(1, endFrame - startFrame + 1) : 0;

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
          {t("videoTrim.current", { frame: currentFrame, time: currentTime.toFixed(2) })}
          {totalFrames !== null && (
            <span style={{ color: "var(--text-dim)", marginLeft: "4px" }}>
              {t("videoTrim.total", { total: totalFrames })}
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
            <option value={24}>24</option>
            <option value={25}>25</option>
            <option value={30}>30</option>
            <option value={50}>50</option>
            <option value={60}>60</option>
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
          {t("videoTrim.stepBackward")}
        </button>
        <button
          type="button"
          onClick={() => stepFrames(1)}
          style={{ fontSize: "11px", padding: "3px 7px" }}
          title="+1 frame"
        >
          {t("videoTrim.stepForward")}
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
          onClick={handleSetStart}
          style={{ fontSize: "11px", padding: "3px 8px", background: "var(--panel)" }}
        >
          {t("videoTrim.setStart", { frame: currentFrame })}
        </button>
        <button
          type="button"
          onClick={handleSetEnd}
          style={{ fontSize: "11px", padding: "3px 8px", background: "var(--panel)" }}
        >
          {t("videoTrim.setEnd", { frame: currentFrame })}
        </button>
        {endFrame > 0 && (
          <button
            type="button"
            onClick={handleResetEnd}
            style={{ fontSize: "11px", padding: "3px 6px", color: "var(--text-dim)" }}
            title={t("videoTrim.toEnd")}
          >
            0 (end)
          </button>
        )}

        <button
          type="button"
          onClick={handlePlayRange}
          style={{
            fontSize: "11px",
            padding: "3px 8px",
            marginLeft: "auto",
            background: isPlayingRange ? "var(--warning)" : "var(--accent)",
            color: "#fff",
            border: "none",
            borderRadius: "4px",
          }}
        >
          {isPlayingRange ? t("videoTrim.pause") : t("videoTrim.playRange")}
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
        {endFrame > 0
          ? t("videoTrim.range", { start: startFrame, end: endFrame, count: frameCount })
          : t("videoTrim.rangeToEnd", { start: startFrame })}
      </div>
    </div>
  );
}
