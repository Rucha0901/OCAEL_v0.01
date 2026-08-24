"use client";

import { useRef, useState } from "react";
import { ArrowRight, BookOpenCheck, FileText, LoaderCircle, RefreshCw, ScanText, Search, Trash2, Upload } from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { useOvael } from "../../../context/OvaelContext";
import type { DocumentRecord, LearningLaunch } from "../../../types";

function bytes(value: number) {
  return value < 1024 * 1024 ? `${Math.max(1, Math.round(value / 1024))} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function launchFor(document: DocumentRecord): LearningLaunch {
  return {
    documentId: document.document_id,
    documentName: document.filename,
    courseId: document.course_id || undefined,
    subjectId: document.subject_id || undefined,
    conceptId: document.focus_concept_id || undefined,
    conceptName: document.focus_concept_name || undefined,
  };
}

export function MaterialsScreen({ onLearn }: { onLearn: (launch: LearningLaunch) => void }) {
  const { documents, courses, subjects, health, refresh, setError } = useOvael();
  const [query, setQuery] = useState("");
  const [courseId, setCourseId] = useState("");
  const [subjectId, setSubjectId] = useState("auto");
  const [uploading, setUploading] = useState(false);
  const [routingId, setRoutingId] = useState("");
  const [uploadKind, setUploadKind] = useState<"material" | "scan">("material");
  const [ocrResult, setOcrResult] = useState<{ document: DocumentRecord; preview: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const visible = documents.filter((document) => document.filename.toLowerCase().includes(query.toLowerCase()));

  const upload = async (file?: File, kind: "material" | "scan" = "material") => {
    if (!file) return;
    setUploading(true); setUploadKind(kind); setOcrResult(null); setError("");
    try {
      const autoRoute = !courseId && subjectId === "auto";
      let teachingCourseId = courseId;
      if (!teachingCourseId && !autoRoute) {
        const subject = subjects.find((item) => item.subject_id === subjectId);
        if (!subject) throw new Error("Choose a teaching subject before uploading this material.");
        const existing = courses.find((course) => course.course_kind === "personal" && course.subject_id === subjectId);
        teachingCourseId = existing?.course_id || (await ovaelApi.createCourse({
          name: `Personal ${subject.name}`,
          subject_id: subjectId,
          description: "Learner-owned material scope",
          course_kind: "personal",
        })).course_id;
      }

      const uploaded = await ovaelApi.uploadDocument(file, teachingCourseId || undefined, autoRoute);
      let preview = "";
      if (file.type.startsWith("image/") && uploaded.status === "ready") {
        const page = await ovaelApi.documentPage(uploaded.document_id, 1);
        preview = page.text.slice(0, 260);
      }
      setOcrResult({ document: uploaded, preview });
      await refresh();
      if (kind === "scan" && uploaded.status === "ready" && uploaded.subject_id && uploaded.focus_concept_id) {
        onLearn(launchFor(uploaded));
      }
    } catch (error) {
      setError(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
      if (scanRef.current) scanRef.current.value = "";
    }
  };

  const detectAndTeach = async (document: DocumentRecord) => {
    setRoutingId(document.document_id); setError("");
    try {
      const routed = await ovaelApi.routeDocument(document.document_id);
      await refresh();
      onLearn(launchFor(routed));
    } catch (error) {
      setError(error instanceof Error ? error.message : "OVAEL could not detect this material's subject.");
    } finally { setRoutingId(""); }
  };

  const remove = async (id: string) => {
    if (!window.confirm("Remove this material and its indexed teaching content?")) return;
    await ovaelApi.deleteDocument(id); await refresh();
  };

  return <div className="page">
    <header className="page-heading materials-heading">
      <div><p className="eyebrow blue">Teaching library</p><h1>Your books and notes become teaching material.</h1><p>Upload a document or scan a printed page. OVAEL reads the content, identifies its learning subject, and keeps the next lesson inside that source.</p></div>
      <div className="upload-controls">
        <select aria-label="Teaching subject" value={subjectId} onChange={(event) => { setSubjectId(event.target.value); setCourseId(""); }} disabled={Boolean(courseId)}><option value="auto">Detect subject from content</option>{subjects.map((subject) => <option key={subject.subject_id} value={subject.subject_id}>{subject.name}</option>)}</select>
        {courses.length > 0 && <select aria-label="Existing course scope" value={courseId} onChange={(event) => setCourseId(event.target.value)}><option value="">No fixed course</option>{courses.map((course) => <option key={course.course_id} value={course.course_id}>{course.name}</option>)}</select>}
        <input ref={inputRef} className="sr-only" type="file" accept=".pdf,.docx,.pptx,.txt,.md,.png,.jpg,.jpeg,.webp" onChange={(event) => void upload(event.target.files?.[0], "material")} />
        <input ref={scanRef} className="sr-only" type="file" accept="image/*" capture="environment" onChange={(event) => void upload(event.target.files?.[0], "scan")} />
        <div className="upload-buttons"><button className="secondary-button" disabled={uploading || !health?.ocr_configured} onClick={() => scanRef.current?.click()}>{uploading && uploadKind === "scan" ? <LoaderCircle className="spin" size={16} /> : <ScanText size={16} />} Scan and teach</button><button className="primary-button" disabled={uploading} onClick={() => inputRef.current?.click()}>{uploading && uploadKind === "material" ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />} Add material</button></div>
      </div>
    </header>

    {ocrResult && <section className="ocr-result" role="status"><ScanText size={20} /><div><strong>{ocrResult.document.subject_name ? `${ocrResult.document.subject_name} detected` : "Material indexed"} · {ocrResult.document.filename}</strong><p>{ocrResult.preview || `Ready to teach ${ocrResult.document.focus_concept_name || "from this source"}.`}</p></div>{ocrResult.document.subject_id && <button className="text-button" onClick={() => onLearn(launchFor(ocrResult.document))}>Teach this now <ArrowRight size={14} /></button>}<button aria-label="Dismiss OCR result" onClick={() => setOcrResult(null)}>×</button></section>}

    <section className="material-toolbar"><label><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search your materials" /></label><span>{documents.length} source{documents.length === 1 ? "" : "s"}</span></section>
    <section className="material-list" aria-live="polite">
      {visible.map((document) => {
        const image = document.content_type.startsWith("image/");
        const readyToTeach = document.status === "ready" && document.subject_id && document.focus_concept_id;
        return <article key={document.document_id}><span className="file-icon">{image ? <ScanText size={21} /> : <FileText size={21} />}</span><div className="material-title"><strong>{document.filename}</strong><small>{document.content_type} · {bytes(document.size_bytes)}</small>{image && <span className={`ocr-badge ${document.status === "ready" ? "ready" : ""}`}>{document.status === "ready" ? "OCR indexed" : "OCR processing"}</span>}</div><div><span className={`material-status ${document.status.includes("fail") ? "review" : document.status === "ready" ? "ready" : "processing"}`}>{document.status.replaceAll("_", " ")}</span><small>{document.subject_name ? `${document.subject_name} · ${document.focus_concept_name || "source grounded"}` : "Subject not detected"}</small></div><div className="material-actions">{readyToTeach ? <button className="text-button" onClick={() => onLearn(launchFor(document))}>Teach me from this <ArrowRight size={14} /></button> : <button className="text-button" disabled={document.status !== "ready" || routingId === document.document_id} onClick={() => void detectAndTeach(document)}>{routingId === document.document_id ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />} Detect and teach</button>}<button className="icon-button" aria-label={`Delete ${document.filename}`} onClick={() => void remove(document.document_id)}><Trash2 size={16} /></button></div></article>;
      })}
      {visible.length === 0 && <div className="empty-library"><BookOpenCheck size={30} /><h2>{query ? "No matching source" : "Add your first teaching source"}</h2><p>PDF, DOCX, PPTX, text, Markdown, and photographed notes are supported up to the backend safety limit.</p></div>}
    </section>
    <div className="capability-note"><ScanText size={20} /><div><strong>{health?.ocr_configured ? "OCR, subject routing, and source indexing are ready" : "OCR needs a configured backend"}</strong><p>Extracted text is attached to the detected subject, course, and focus concept before Learn or Chat can use it.</p></div></div>
  </div>;
}
