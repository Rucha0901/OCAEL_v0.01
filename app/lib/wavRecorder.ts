export interface WavRecording {
  stop(): Promise<Blob>;
}

function encodeWav(chunks: Float32Array[], sampleRate: number) {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, value: string) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  write(0, "RIFF"); view.setUint32(4, 36 + length * 2, true); write(8, "WAVE"); write(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  write(36, "data"); view.setUint32(40, length * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const sample of chunk) { const bounded = Math.max(-1, Math.min(1, sample)); view.setInt16(offset, bounded < 0 ? bounded * 0x8000 : bounded * 0x7fff, true); offset += 2; }
  return new Blob([view], { type: "audio/wav" });
}

export async function startWavRecording(): Promise<WavRecording> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  source.connect(processor); processor.connect(context.destination);
  return { async stop() { processor.disconnect(); source.disconnect(); stream.getTracks().forEach((track) => track.stop()); const blob = encodeWav(chunks, context.sampleRate); await context.close(); return blob; } };
}
