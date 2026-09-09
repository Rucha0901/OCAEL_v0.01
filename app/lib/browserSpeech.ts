export interface BrowserSpeechHandle {
  stop(): void;
  abort(): void;
}

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

function speechRecognitionConstructor() {
  if (typeof window === "undefined") return undefined;
  const speechWindow = window as typeof window & {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };
  return speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition;
}

export function browserSpeechInputAvailable() {
  return Boolean(speechRecognitionConstructor());
}

export function startBrowserSpeechInput(options: {
  language?: string;
  onTranscript(text: string, final: boolean): void;
  onEnd(finalText: string): void;
  onError(message: string): void;
}): BrowserSpeechHandle {
  const Recognition = speechRecognitionConstructor();
  if (!Recognition) throw new Error("Speech recognition is not available in this browser.");
  const recognition = new Recognition();
  let finalText = "";
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = options.language || "en-IN";
  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      if (result.isFinal) finalText += `${result[0].transcript} `;
      else interim += result[0].transcript;
    }
    options.onTranscript(`${finalText}${interim}`.trim(), Boolean(finalText));
  };
  recognition.onerror = (event) => options.onError(event.error || "Speech recognition stopped unexpectedly.");
  recognition.onend = () => options.onEnd(finalText.trim());
  recognition.start();
  return { stop: () => recognition.stop(), abort: () => recognition.abort() };
}
