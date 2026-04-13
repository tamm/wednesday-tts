export const WednesdayTTSPlugin = async ({ directory, worktree, $ }) => {
  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return;
      if (isMuted()) return;
      
      const msg = event.data?.message;
      if (!msg?.content) return;
      
      const text = typeof msg.content === "string" 
        ? msg.content.trim() 
        : msg.content.find(p => p.type === "text")?.text?.trim();
      
      if (!text || text.length < 5) return;
      
      const voiceHash = await computeVoiceHash(directory);
      const payload = JSON.stringify({
        command: "speak",
        text,
        normalization: "markdown",
        voice_hash: voiceHash,
        pan: 0.5,
        timestamp: Date.now() / 1000,
      });
      
      await $`python3 -c "import socket; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(1); s.connect('/tmp/tts-daemon.sock'); s.sendall('${payload}'.encode())"`.text();
    },
  };
};

function isMuted() {
  try {
    require("fs").accessSync("/tmp/tts-mute");
    return true;
  } catch {
    return process.env.TTS_MUTE === "1";
  }
}

async function computeVoiceHash(cwd) {
  const crypto = await import("crypto");
  try {
    const gitRoot = await $`git -C ${cwd} rev-parse --show-toplevel 2>/dev/null`.text();
    return crypto.createHash("sha256").update(gitRoot.trim() || cwd).digest("hex").slice(0, 8);
  } catch {
    return crypto.createHash("sha256").update(cwd).digest("hex").slice(0, 8);
  }
}
