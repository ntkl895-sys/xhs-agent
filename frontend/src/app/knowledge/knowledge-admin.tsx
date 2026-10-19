"use client";

/**
 * 知识库人工维护组件（第 19 步 · 知识库可人工维护）。
 *
 * 本文件承载 /knowledge 页新增的四块能力（页面壳与原有三 Tab 在 page.tsx）：
 *
 * ① {@link ManualCreateDialog}「新建知识块」Dialog：六类型 + 标题 / 正文 +
 *    （违禁词）类别 + 可选关联人设；`forbidden_word` 无需向量化（离线可维护），
 *    其它类型向量化失败（如依赖未装）由后端 400 如实告知（**零写入**）；
 * ② {@link EditSnippetDialog}「编辑」Dialog：改标题 / 正文；正文变化触发后端
 *    重算内容指纹并重新向量化；**重向量化失败会置空向量，但正文照常保存**
 *    （响应 `warnings` 非阻断提示，页面用 Notice 展示）；
 * ③ {@link ForbiddenAdminBar} 违禁词管理条（知识块 Tab 选类型 = 违禁词时显示）：
 *    「同步内置词表(seed)」幂等按钮 + 「新增违禁词」入口（复用创建 Dialog）；
 *    归档 / 恢复沿用列表行的既有操作；
 * ④ {@link ImportTab}「文档导入」Tab：文本粘贴或选择 .txt / .md（FormData 上传），
 *    可选目标类型（通用 / 人设知识）与切分参数；结果如实展示
 *    切分总数 / 写入 / 跳过 / 失败与 warnings。
 *
 * 红线延续：知识块只增 / 归档（无物理删除）；浏览器只请求本站 /api/v1/**；
 * 响应与页面均不含 embedding 向量、不含 ``llm_raw``、不含密钥与绝对路径。
 */

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ErrorText, Field, Notice, toApiError } from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  ChunkTypeName,
  ImportResult,
  ImportTextRequest,
  KnowledgeSnippet,
  Page,
  Persona,
  SeedForbiddenWordsResult,
  SnippetManualCreateRequest,
  SnippetUpdateRequest,
  SnippetWriteResult,
} from "@/lib/types";

/* ------------------------------------------------------------------ 常量 */

/** 手动创建的六类型选项（与后端 ChunkType 对齐） */
const CHUNK_TYPE_OPTIONS: { value: ChunkTypeName; label: string }[] = [
  { value: "general", label: "通用" },
  { value: "persona_knowledge", label: "人设知识" },
  { value: "forbidden_word", label: "违禁词" },
  { value: "topic_template", label: "选题模板" },
  { value: "hit_pattern", label: "爆款规律" },
  { value: "review_insight", label: "复盘心得" },
];

/** 文档导入的目标类型（后端只允许这两类） */
const IMPORT_TYPE_OPTIONS: { value: "general" | "persona_knowledge"; label: string }[] = [
  { value: "general", label: "通用（general）" },
  { value: "persona_knowledge", label: "人设知识（persona_knowledge）" },
];

/** 人设下拉的「不关联」哨兵值（base-ui Select 不接受空串 value） */
const NO_PERSONA = "__none__";

/** 正文 / 标题长度上限（与后端 knowledge_admin 常量一致） */
const CONTENT_MAX_LEN = 20000;
const TITLE_MAX_LEN = 200;

/* ------------------------------------------------------------------ 工具 / 钩子 */

function personaLabel(persona: Persona): string {
  return persona.account_name ? `${persona.name}（${persona.account_name}）` : persona.name;
}

/**
 * 加载 active 人设列表（用于「关联人设」下拉）。
 *
 * 仅在 `enabled=true` 时拉取（Dialog 打开 / 导入 Tab 挂载）；
 * 首次 `await` 之前不做 `setState`（`react-hooks/set-state-in-effect`）。
 */
function usePersonaOptions(enabled: boolean) {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<Persona>>(
          `/api/v1/personas${toQuery({ status: "active", page: 1, page_size: 100 })}`,
        );
        if (!cancelled) {
          setPersonas(data.items);
          setError(null);
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(toApiError(err));
          setLoading(false);
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { personas, loading, error };
}

/** 400 错误的人话提示（向量化未就绪 / 参数非法） */
function FourHundredHint({ error, forbiddenSafe }: { error: ApiError | null; forbiddenSafe: boolean }) {
  if (!error || error.status !== 400) return null;
  return (
    <p className="text-xs text-muted-foreground">
      提示：400 通常表示向量化未就绪（依赖未安装 / 模型未下载）或参数不合法。
      {forbiddenSafe ? "「违禁词」类型无需向量化，可离线维护。" : ""}
    </p>
  );
}

/* ------------------------------------------------------------------ 新建知识块 Dialog */

export function ManualCreateDialog({
  open,
  onOpenChange,
  presetChunkType = "general",
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 打开时的预设类型（如违禁词管理条传 forbidden_word） */
  presetChunkType?: ChunkTypeName;
  onCreated: (result: SnippetWriteResult) => void;
}) {
  const [chunkType, setChunkType] = useState<ChunkTypeName>(presetChunkType);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("");
  const [personaId, setPersonaId] = useState(NO_PERSONA);
  const [fieldError, setFieldError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);

  const { personas, loading: loadingPersonas, error: personaError } = usePersonaOptions(open);

  const isForbidden = chunkType === "forbidden_word";
  const isPersonaKnowledge = chunkType === "persona_knowledge";

  const personaItems = {
    [NO_PERSONA]: "不关联人设（可选）",
    ...Object.fromEntries(personas.map((persona) => [persona.id, personaLabel(persona)])),
  };

  const submit = useCallback(async () => {
    const text = content.trim();
    if (!text) {
      setFieldError(isForbidden ? "请填写违禁词（正文即词本身）" : "请填写正文");
      return;
    }
    if (text.length > CONTENT_MAX_LEN) {
      setFieldError(`正文超过 ${CONTENT_MAX_LEN} 字上限`);
      return;
    }
    if (title.trim().length > TITLE_MAX_LEN) {
      setFieldError(`标题超过 ${TITLE_MAX_LEN} 字上限`);
      return;
    }
    if (isPersonaKnowledge && personaId === NO_PERSONA) {
      setFieldError("人设知识必须关联一个人设（未关联的块不会被反哺检索纳入）");
      return;
    }
    setFieldError("");
    setSubmitting(true);
    setSubmitError(null);
    try {
      const body: SnippetManualCreateRequest = {
        chunk_type: chunkType,
        content: text,
        title: title.trim() ? title.trim() : null,
      };
      if (isForbidden) body.category = category.trim() ? category.trim() : null;
      if (personaId !== NO_PERSONA) {
        body.ref_type = "persona";
        body.ref_id = personaId;
      }
      const result = await apiClient.post<SnippetWriteResult>(
        "/api/v1/knowledge/manual",
        body,
      );
      onOpenChange(false);
      onCreated(result);
    } catch (err) {
      setSubmitError(toApiError(err));
    } finally {
      setSubmitting(false);
    }
  }, [
    content,
    title,
    category,
    personaId,
    chunkType,
    isForbidden,
    isPersonaKnowledge,
    onOpenChange,
    onCreated,
  ]);

  return (
    <Dialog open={open} onOpenChange={(next) => (!next && !submitting ? onOpenChange(false) : undefined)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>新建知识块</DialogTitle>
          <DialogDescription>
            人工录入一条知识块并写入知识库（只增 / 归档，无删除）。
            违禁词用于创作图精确匹配、无需向量化；其它类型会先向量化，失败则整体不写入。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label="类型 *" htmlFor="kc-type">
            <Select
              items={Object.fromEntries(CHUNK_TYPE_OPTIONS.map((item) => [item.value, item.label]))}
              value={chunkType}
              onValueChange={(value) => setChunkType(String(value) as ChunkTypeName)}
              disabled={submitting}
            >
              <SelectTrigger id="kc-type" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CHUNK_TYPE_OPTIONS.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="标题（可选）" htmlFor="kc-title" hint={`最多 ${TITLE_MAX_LEN} 字`}>
            <Input
              id="kc-title"
              value={title}
              maxLength={TITLE_MAX_LEN}
              disabled={submitting}
              placeholder="例如：通勤穿搭开场话术"
              onChange={(event) => setTitle(event.target.value)}
            />
          </Field>

          <Field
            label={isForbidden ? "违禁词 *" : "正文 *"}
            htmlFor="kc-content"
            hint={
              isForbidden
                ? "填写词本身（如「最便宜」）；扫描为精确匹配，不向量化"
                : `知识块正文，最多 ${CONTENT_MAX_LEN} 字；保存时自动向量化`
            }
          >
            <Textarea
              id="kc-content"
              value={content}
              rows={isForbidden ? 2 : 5}
              maxLength={CONTENT_MAX_LEN}
              disabled={submitting}
              placeholder={isForbidden ? "例如：全网最低价" : "写一段可复用的经验 / 话术 / 模板……"}
              onChange={(event) => setContent(event.target.value)}
            />
          </Field>

          {isForbidden ? (
            <Field label="类别（可选）" htmlFor="kc-category" hint="缺省 manual；用于列表归类展示">
              <Input
                id="kc-category"
                value={category}
                maxLength={64}
                disabled={submitting}
                placeholder="例如：诱导交易 / 极限词"
                onChange={(event) => setCategory(event.target.value)}
              />
            </Field>
          ) : null}

          <Field
            label={isPersonaKnowledge ? "关联人设 *" : "关联人设（可选）"}
            htmlFor="kc-persona"
            hint={
              isPersonaKnowledge
                ? "人设知识必须关联人设：选题 / 创作反哺时按当前人设检索"
                : loadingPersonas
                  ? "正在加载人设……"
                  : undefined
            }
          >
            <Select
              items={personaItems}
              value={personaId}
              onValueChange={(value) => setPersonaId(String(value))}
              disabled={submitting || loadingPersonas}
            >
              <SelectTrigger id="kc-persona" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PERSONA} disabled={isPersonaKnowledge}>
                  {isPersonaKnowledge ? "不关联人设（人设知识不允许）" : "不关联人设（可选）"}
                </SelectItem>
                {personas.length === 0 ? (
                  <SelectItem value="__empty_persona__" disabled>
                    暂无可用人设
                  </SelectItem>
                ) : (
                  personas.map((persona) => (
                    <SelectItem key={persona.id} value={persona.id}>
                      {personaLabel(persona)}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </Field>

          {personaError ? <ErrorText error={personaError} /> : null}
          {fieldError ? <p className="text-xs text-red-600">{fieldError}</p> : null}
          <ErrorText error={submitError} />
          <FourHundredHint error={submitError} forbiddenSafe />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button onClick={() => void submit()} disabled={submitting || loadingPersonas}>
            {submitting ? "保存中…" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ 编辑 Dialog */

export function EditSnippetDialog({
  snippet,
  open,
  onOpenChange,
  onSaved,
}: {
  snippet: KnowledgeSnippet;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (result: SnippetWriteResult) => void;
}) {
  const [title, setTitle] = useState(snippet.title ?? "");
  const [content, setContent] = useState(snippet.content);
  const [fieldError, setFieldError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ApiError | null>(null);

  const isForbidden = snippet.chunk_type === "forbidden_word";
  const contentChanged = content.trim() !== snippet.content;

  const submit = useCallback(async () => {
    const text = content.trim();
    if (!text) {
      setFieldError(isForbidden ? "违禁词不能为空" : "正文不能为空");
      return;
    }
    if (text.length > CONTENT_MAX_LEN) {
      setFieldError(`正文超过 ${CONTENT_MAX_LEN} 字上限`);
      return;
    }
    if (title.trim().length > TITLE_MAX_LEN) {
      setFieldError(`标题超过 ${TITLE_MAX_LEN} 字上限`);
      return;
    }
    setFieldError("");
    setSaving(true);
    setSaveError(null);
    try {
      const patch: SnippetUpdateRequest = { title: title.trim(), content: text };
      const result = await apiClient.patch<SnippetWriteResult>(
        `/api/v1/knowledge/${snippet.id}`,
        patch,
      );
      onOpenChange(false);
      onSaved(result);
    } catch (err) {
      setSaveError(toApiError(err));
    } finally {
      setSaving(false);
    }
  }, [title, content, isForbidden, snippet.id, onOpenChange, onSaved]);

  return (
    <Dialog open={open} onOpenChange={(next) => (!next && !saving ? onOpenChange(false) : undefined)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>编辑知识块</DialogTitle>
          <DialogDescription>
            修改标题 / 正文后保存。
            {isForbidden
              ? "违禁词为精确匹配，无需向量化。"
              : "正文有变化时后端会自动重算内容指纹并重新向量化；若重向量化失败，正文照常保存、向量置空并给出提示。"}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label="标题（可选）" htmlFor="ke-title" hint={`最多 ${TITLE_MAX_LEN} 字；留空 = 清空标题`}>
            <Input
              id="ke-title"
              value={title}
              maxLength={TITLE_MAX_LEN}
              disabled={saving}
              onChange={(event) => setTitle(event.target.value)}
            />
          </Field>

          <Field
            label={isForbidden ? "违禁词 *" : "正文 *"}
            htmlFor="ke-content"
            hint={
              isForbidden
                ? "修改后将更新词表（content_hash 同步重算）"
                : contentChanged
                  ? "检测到正文变化：保存将触发重新向量化"
                  : "正文未变化：不会触发重新向量化"
            }
          >
            <Textarea
              id="ke-content"
              value={content}
              rows={isForbidden ? 2 : 6}
              maxLength={CONTENT_MAX_LEN}
              disabled={saving}
              onChange={(event) => setContent(event.target.value)}
            />
          </Field>

          {fieldError ? <p className="text-xs text-red-600">{fieldError}</p> : null}
          <ErrorText error={saveError} />
          <FourHundredHint error={saveError} forbiddenSafe />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            取消
          </Button>
          <Button onClick={() => void submit()} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ 违禁词管理条 */

export function ForbiddenAdminBar({
  onRefresh,
  onAddWord,
}: {
  /** seed 成功后刷新列表 */
  onRefresh: () => void;
  /** 「新增违禁词」入口（父级打开创建 Dialog，预设 forbidden_word） */
  onAddWord: () => void;
}) {
  const [seeding, setSeeding] = useState(false);
  const [seedNotice, setSeedNotice] = useState("");
  const [seedError, setSeedError] = useState<ApiError | null>(null);

  const runSeed = useCallback(async () => {
    setSeeding(true);
    setSeedError(null);
    setSeedNotice("");
    try {
      const result = await apiClient.post<SeedForbiddenWordsResult>(
        "/api/v1/knowledge/seed-forbidden-words",
        {},
      );
      setSeedNotice(
        `同步完成：新增 ${result.inserted} 条 / 复用（已存在）${result.reused} 条，` +
          `内置词表共 ${result.total} 词。可重复点击（幂等）。`,
      );
      onRefresh();
    } catch (err) {
      setSeedError(toApiError(err));
    } finally {
      setSeeding(false);
    }
  }, [onRefresh]);

  return (
    <Card>
      <CardContent className="space-y-3 py-4">
        <p className="text-sm text-muted-foreground">
          违禁词用于创作图的「精确匹配」合规检查（不向量化、离线可维护）。
          「同步内置词表」把内置词表幂等写入知识库（已存在的词自动跳过）；
          新增词走「新增违禁词」，归档 / 恢复用下方列表行的按钮。
          词表为空或读取失败时，创作图自动回退内置词表（保守兜底），不会跳过检查。
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" onClick={() => void runSeed()} disabled={seeding}>
            {seeding ? "同步中…" : "同步内置词表（seed）"}
          </Button>
          <Button size="sm" variant="outline" onClick={onAddWord} disabled={seeding}>
            新增违禁词
          </Button>
          <span className="text-xs text-muted-foreground">
            提示：归档一条违禁词后，创作图扫描立即不再命中该词。
          </span>
        </div>
        {seedNotice ? <Notice text={seedNotice} /> : null}
        <ErrorText error={seedError} />
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ 文档导入 Tab */

export function ImportTab({ onImported }: { onImported: () => void }) {
  const [mode, setMode] = useState<"text" | "file">("text");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [chunkType, setChunkType] = useState<"general" | "persona_knowledge">("general");
  const [personaId, setPersonaId] = useState(NO_PERSONA);
  const [chunkSize, setChunkSize] = useState("");
  const [chunkOverlap, setChunkOverlap] = useState("");
  const [fieldError, setFieldError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);

  const { personas, loading: loadingPersonas, error: personaError } = usePersonaOptions(true);

  const isPersonaKnowledge = chunkType === "persona_knowledge";

  const personaItems = {
    [NO_PERSONA]: "请选择人设（必选）",
    ...Object.fromEntries(personas.map((persona) => [persona.id, personaLabel(persona)])),
  };

  const run = useCallback(async () => {
    setFieldError("");
    setNotice("");
    setError(null);
    setResult(null);

    if (mode === "text" && !text.trim()) {
      setFieldError("请粘贴要导入的文本");
      return;
    }
    if (mode === "file" && !file) {
      setFieldError("请选择 .txt / .md 文件");
      return;
    }
    if (isPersonaKnowledge && personaId === NO_PERSONA) {
      setFieldError("人设知识必须选择一个人设");
      return;
    }
    if (chunkSize.trim()) {
      const size = Number(chunkSize.trim());
      if (!Number.isInteger(size) || size < 200 || size > 4000) {
        setFieldError("切分块大小需为 200~4000 之间的整数（留空用默认 800）");
        return;
      }
    }
    if (chunkOverlap.trim()) {
      const overlap = Number(chunkOverlap.trim());
      if (!Number.isInteger(overlap) || overlap < 0 || overlap > 500) {
        setFieldError("块间重叠需为 0~500 之间的整数（留空用默认 100）");
        return;
      }
    }

    setBusy(true);
    try {
      let res: ImportResult;
      if (mode === "text") {
        const body: ImportTextRequest = { content: text, chunk_type: chunkType };
        if (isPersonaKnowledge) body.ref_id = personaId;
        if (chunkSize.trim()) body.chunk_size = Number(chunkSize.trim());
        if (chunkOverlap.trim()) body.chunk_overlap = Number(chunkOverlap.trim());
        res = await apiClient.post<ImportResult>("/api/v1/knowledge/import-text", body);
      } else {
        const picked = file;
        if (!picked) {
          return; // 理论不可达（上方已校验）；finally 复位 busy
        }
        const formData = new FormData();
        formData.append("file", picked);
        formData.append("chunk_type", chunkType);
        if (isPersonaKnowledge && personaId !== NO_PERSONA) {
          formData.append("ref_id", personaId);
        }
        if (chunkSize.trim()) formData.append("chunk_size", chunkSize.trim());
        if (chunkOverlap.trim()) formData.append("chunk_overlap", chunkOverlap.trim());
        res = await apiClient.postForm<ImportResult>("/api/v1/knowledge/import-file", formData);
      }
      setResult(res);
      setNotice(
        `导入完成：切分 ${res.total_chunks} 块 / 写入 ${res.inserted} / ` +
          `跳过（已存在）${res.skipped} / 失败 ${res.failed}。`,
      );
      onImported();
    } catch (err) {
      const apiErr = toApiError(err);
      setError(apiErr);
      if (apiErr.status === 400) {
        setNotice(
          "提示：400 通常表示向量化未就绪（依赖未安装 / 模型未下载）、参数不合法，" +
            "或文件不是 UTF-8 / 超过 1MB / 非 .txt、.md。请检查后端与文件后重试。",
        );
      }
    } finally {
      setBusy(false);
    }
  }, [
    mode,
    text,
    file,
    chunkType,
    isPersonaKnowledge,
    personaId,
    chunkSize,
    chunkOverlap,
    onImported,
  ]);

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-4 py-5">
          <p className="text-sm text-muted-foreground">
            把长文档切分后批量写入知识库（向量化存储，供反哺检索使用）。
            支持粘贴文本或上传 UTF-8 的 .txt / .md（≤1MB）；本步不做 .docx / .pdf
            解析、不做网页抓取。查重先于向量化：与库内已有内容指纹相同的块会被跳过。
          </p>

          <div className="flex flex-wrap items-end gap-3">
            <div className="w-40">
              <Field label="导入方式" htmlFor="ki-mode">
                <Select
                  items={{ text: "粘贴文本", file: "上传文件（.txt / .md）" }}
                  value={mode}
                  onValueChange={(value) => setMode(String(value) === "file" ? "file" : "text")}
                  disabled={busy}
                >
                  <SelectTrigger id="ki-mode" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="text">粘贴文本</SelectItem>
                    <SelectItem value="file">上传文件（.txt / .md）</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <div className="w-56">
              <Field label="导入为" htmlFor="ki-type">
                <Select
                  items={Object.fromEntries(IMPORT_TYPE_OPTIONS.map((item) => [item.value, item.label]))}
                  value={chunkType}
                  onValueChange={(value) =>
                    setChunkType(String(value) === "persona_knowledge" ? "persona_knowledge" : "general")
                  }
                  disabled={busy}
                >
                  <SelectTrigger id="ki-type" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {IMPORT_TYPE_OPTIONS.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </div>
            {isPersonaKnowledge ? (
              <div className="min-w-56 flex-1">
                <Field
                  label="关联人设 *"
                  htmlFor="ki-persona"
                  hint={loadingPersonas ? "正在加载人设……" : "人设知识导入后仅对该人设的反哺检索可见"}
                >
                  <Select
                    items={personaItems}
                    value={personaId}
                    onValueChange={(value) => setPersonaId(String(value))}
                    disabled={busy || loadingPersonas}
                  >
                    <SelectTrigger id="ki-persona" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {personas.length === 0 ? (
                        <SelectItem value={NO_PERSONA} disabled>
                          暂无可用人设
                        </SelectItem>
                      ) : (
                        personas.map((persona) => (
                          <SelectItem key={persona.id} value={persona.id}>
                            {personaLabel(persona)}
                          </SelectItem>
                        ))
                      )}
                    </SelectContent>
                  </Select>
                </Field>
              </div>
            ) : null}
            <div className="w-32">
              <Field label="块大小" htmlFor="ki-size" hint="200~4000；留空 800">
                <Input
                  id="ki-size"
                  value={chunkSize}
                  inputMode="numeric"
                  placeholder="800"
                  disabled={busy}
                  onChange={(event) => setChunkSize(event.target.value)}
                />
              </Field>
            </div>
            <div className="w-32">
              <Field label="块间重叠" htmlFor="ki-overlap" hint="0~500；留空 100">
                <Input
                  id="ki-overlap"
                  value={chunkOverlap}
                  inputMode="numeric"
                  placeholder="100"
                  disabled={busy}
                  onChange={(event) => setChunkOverlap(event.target.value)}
                />
              </Field>
            </div>
          </div>

          {mode === "text" ? (
            <Field
              label="文本内容 *"
              htmlFor="ki-text"
              hint="最多 20 万字符；按空行 / 段落 / 句子递归切分"
            >
              <Textarea
                id="ki-text"
                value={text}
                rows={10}
                disabled={busy}
                placeholder={"粘贴要导入的长文本……\n\n支持 Markdown；会按段落与句子递归切分为知识块。"}
                onChange={(event) => setText(event.target.value)}
              />
            </Field>
          ) : (
            <Field
              label="选择文件 *"
              htmlFor="ki-file"
              hint="仅 .txt / .md，UTF-8 编码，≤1MB；不合规会被后端明确拒绝"
            >
              <Input
                id="ki-file"
                type="file"
                accept=".txt,.md,text/plain,text/markdown"
                disabled={busy}
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </Field>
          )}

          {personaError ? <ErrorText error={personaError} /> : null}
          {fieldError ? <p className="text-xs text-red-600">{fieldError}</p> : null}

          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => void run()} disabled={busy}>
              {busy ? "导入中…（最长约 2 分钟）" : "开始导入"}
            </Button>
            <span className="text-xs text-muted-foreground">
              逐块向量化；个别块失败只会计入「失败」计数，不影响其它块入库。
            </span>
          </div>

          {notice ? <Notice text={notice} /> : null}
          <ErrorText error={error} />
        </CardContent>
      </Card>

      {result ? (
        <Card>
          <CardContent className="space-y-3 py-4 text-sm">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div>
                <div className="text-muted-foreground">切分总块数</div>
                <div className="text-xl font-semibold">{result.total_chunks}</div>
              </div>
              <div>
                <div className="text-muted-foreground">写入</div>
                <div className="text-xl font-semibold">{result.inserted}</div>
              </div>
              <div>
                <div className="text-muted-foreground">跳过（已存在）</div>
                <div className="text-xl font-semibold">{result.skipped}</div>
              </div>
              <div>
                <div className="text-muted-foreground">失败</div>
                <div className="text-xl font-semibold">{result.failed}</div>
              </div>
            </div>
            <div className="text-muted-foreground">
              切分实现：{result.splitter}；块大小 {result.chunk_size} / 重叠{" "}
              {result.chunk_overlap}
            </div>
            {result.warnings.length ? (
              <div>
                <div className="mb-1 font-medium">失败明细（如实计数）</div>
                <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                  {result.warnings.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
