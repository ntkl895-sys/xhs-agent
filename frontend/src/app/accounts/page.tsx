"use client";

/**
 * 「账号与人设」页面（第 8 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI /api/v1/**
 * 浏览器永远拿不到后端地址。
 *
 * 两个页签各自维护筛选 / 分页 / 表单状态；表单为受控组件 + 手写校验，
 * 不引入任何表单库。所有「删除」都是归档（PATCH status=archived）。
 *
 * 数据获取约定：请求放在 effect 里的异步 IIFE 中，**首次 await 之前不做 setState**，
 * 加载态由事件处理器显式打开（既满足 react-hooks/set-state-in-effect，也避免级联渲染）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import {
  ConfirmDialog,
  ErrorText,
  Field,
  INITIAL_LOAD,
  Notice,
  Pagination,
  TagInput,
  formatDateTime,
  toApiError,
} from "@/components/shared";
import type { LoadState } from "@/components/shared";
import type { Account, AccountWrite, Page, Persona, PersonaWrite } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 10;
const PUBLIC_SCOPE = "__public__";

function StatusBadge({ status }: { status: string }) {
  const archived = status === "archived";
  return (
    <Badge
      className={cn(
        archived
          ? "border-amber-600/30 bg-amber-500/15 text-amber-700"
          : "border-emerald-600/30 bg-emerald-500/15 text-emerald-700",
      )}
    >
      {archived ? "已归档" : "在用"}
    </Badge>
  );
}

/* ------------------------------------------------------------------ 账号页签 */

interface AccountQuery {
  page: number;
  status: string;
  q: string;
}

interface AccountFormState {
  name: string;
  xhs_red_id: string;
  niche: string;
  remark: string;
}

const EMPTY_ACCOUNT_FORM: AccountFormState = { name: "", xhs_red_id: "", niche: "", remark: "" };

function AccountsTab() {
  const [query, setQuery] = useState<AccountQuery>({ page: 1, status: "active", q: "" });
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<LoadState<Account>>(INITIAL_LOAD);

  const [qInput, setQInput] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<Account | null>(null);
  const [form, setForm] = useState<AccountFormState>(EMPTY_ACCOUNT_FORM);
  const [formError, setFormError] = useState<ApiError | null>(null);
  const [formFieldErrors, setFormFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const [confirmTarget, setConfirmTarget] = useState<Account | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<Account>>(
          `/api/v1/accounts${toQuery({
            page: query.page,
            page_size: PAGE_SIZE,
            status: query.status,
            q: query.q,
          })}`,
        );
        if (!cancelled) setState({ loading: false, data, error: null });
      } catch (err) {
        if (!cancelled) setState({ loading: false, data: null, error: toApiError(err) });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [query, reloadToken]);

  /** 打开加载态并应用新的查询条件（始终重新拉取，即便条件未变） */
  const applyQuery = useCallback((patch: Partial<AccountQuery>) => {
    setState((prev) => ({ ...prev, loading: true }));
    setQuery((prev) => ({ ...prev, ...patch }));
    setReloadToken((token) => token + 1);
  }, []);

  const reload = useCallback(() => {
    setState((prev) => ({ ...prev, loading: true }));
    setReloadToken((token) => token + 1);
  }, []);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_ACCOUNT_FORM);
    setFormError(null);
    setFormFieldErrors({});
    setDialogOpen(true);
  };

  const openEdit = (account: Account) => {
    setEditing(account);
    setForm({
      name: account.name,
      xhs_red_id: account.xhs_red_id ?? "",
      niche: account.niche ?? "",
      remark: account.remark ?? "",
    });
    setFormError(null);
    setFormFieldErrors({});
    setDialogOpen(true);
  };

  const submit = async () => {
    const fieldErrors: Record<string, string> = {};
    const name = form.name.trim();
    if (!name) fieldErrors.name = "名称不能为空";
    else if (name.length > 100) fieldErrors.name = "名称不超过 100 字";
    if (form.xhs_red_id.trim().length > 64) fieldErrors.xhs_red_id = "小红书号不超过 64 字";
    if (form.niche.trim().length > 100) fieldErrors.niche = "领域不超过 100 字";
    setFormFieldErrors(fieldErrors);
    if (Object.keys(fieldErrors).length > 0) return;

    const payload: AccountWrite = {
      name,
      xhs_red_id: form.xhs_red_id.trim() || null,
      niche: form.niche.trim() || null,
      remark: form.remark.trim() || null,
    };

    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        await apiClient.patch<Account>(`/api/v1/accounts/${editing.id}`, payload);
        setNotice("已更新账号");
      } else {
        await apiClient.post<Account>("/api/v1/accounts", payload);
        setNotice("已新建账号");
      }
      setDialogOpen(false);
      reload();
    } catch (err) {
      setFormError(toApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const changeStatus = async (account: Account, next: "active" | "archived") => {
    setConfirmBusy(true);
    setActionError(null);
    try {
      await apiClient.patch<Account>(`/api/v1/accounts/${account.id}`, { status: next });
      setNotice(next === "archived" ? "已归档账号" : "已恢复账号");
      setConfirmTarget(null);
      reload();
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setConfirmBusy(false);
    }
  };

  const rows = state.data?.items ?? [];

  return (
    <div className="flex flex-col gap-4">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          applyQuery({ q: qInput.trim(), page: 1 });
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="account-q">关键词</Label>
          <Input
            id="account-q"
            value={qInput}
            placeholder="按账号名模糊匹配"
            onChange={(event) => setQInput(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="account-status">状态</Label>
          <Select
            value={query.status}
            onValueChange={(value) => applyQuery({ status: String(value), page: 1 })}
          >
            <SelectTrigger id="account-status" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="active">在用</SelectItem>
              <SelectItem value="archived">已归档</SelectItem>
              <SelectItem value="all">全部</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <Button type="submit" variant="outline">
          查询
        </Button>
        <div className="ml-auto flex items-center gap-2">
          <Button type="button" onClick={openCreate}>
            新建账号
          </Button>
        </div>
      </form>

      <ErrorText error={actionError} />
      <Notice text={notice} />

      <Card>
        <CardContent className="pt-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>小红书号</TableHead>
                <TableHead>领域</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.loading ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : state.error ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center">
                    <ErrorText error={state.error} />
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                    暂无数据（可切换状态或新建账号）
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((account) => (
                  <TableRow key={account.id}>
                    <TableCell className="font-medium">{account.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {account.xhs_red_id ?? "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{account.niche ?? "—"}</TableCell>
                    <TableCell>
                      <StatusBadge status={account.status} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDateTime(account.created_at)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1.5">
                        <Button variant="outline" size="sm" onClick={() => openEdit(account)}>
                          编辑
                        </Button>
                        {account.status === "active" ? (
                          <Button
                            variant="destructive"
                            size="sm"
                            onClick={() => setConfirmTarget(account)}
                          >
                            归档
                          </Button>
                        ) : (
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => void changeStatus(account, "active")}
                          >
                            恢复
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <Pagination
            page={query.page}
            pageSize={PAGE_SIZE}
            total={state.data?.total ?? 0}
            loading={state.loading}
            onPage={(next) => applyQuery({ page: next })}
          />
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑账号" : "新建账号"}</DialogTitle>
            <DialogDescription>
              只保存你手填的备注信息；本工具不登录小红书、不保存任何登录态。
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-3">
            <Field label="名称 *" htmlFor="account-name" error={formFieldErrors.name}>
              <Input
                id="account-name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </Field>
            <Field
              label="小红书号"
              htmlFor="account-red-id"
              error={formFieldErrors.xhs_red_id}
              hint="手填，唯一；留空表示不填"
            >
              <Input
                id="account-red-id"
                value={form.xhs_red_id}
                onChange={(event) => setForm({ ...form, xhs_red_id: event.target.value })}
              />
            </Field>
            <Field label="领域" htmlFor="account-niche" error={formFieldErrors.niche}>
              <Input
                id="account-niche"
                value={form.niche}
                onChange={(event) => setForm({ ...form, niche: event.target.value })}
              />
            </Field>
            <Field label="备注" htmlFor="account-remark">
              <Textarea
                id="account-remark"
                value={form.remark}
                onChange={(event) => setForm({ ...form, remark: event.target.value })}
              />
            </Field>
            <ErrorText error={formError} />
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)} disabled={saving}>
              取消
            </Button>
            <Button onClick={() => void submit()} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="确认归档该账号？"
        description={`归档后「${confirmTarget?.name ?? ""}」将不在默认列表显示，其名下的人设会保留不变。可随时恢复。`}
        confirmText="确认归档"
        busy={confirmBusy}
        onCancel={() => setConfirmTarget(null)}
        onConfirm={() => confirmTarget && void changeStatus(confirmTarget, "archived")}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ 人设页签 */

interface PersonaQuery {
  page: number;
  status: string;
  scope: string;
  q: string;
}

interface PersonaFormState {
  name: string;
  accountId: string;
  positioning: string;
  targetAudience: string;
  tone: string;
  contentPillars: string[];
  taboo: string[];
  signatureStyle: string;
  systemPrompt: string;
  isDefault: boolean;
}

const EMPTY_PERSONA_FORM: PersonaFormState = {
  name: "",
  accountId: PUBLIC_SCOPE,
  positioning: "",
  targetAudience: "",
  tone: "",
  contentPillars: [],
  taboo: [],
  signatureStyle: "",
  systemPrompt: "",
  isDefault: false,
};

function PersonasTab() {
  const [query, setQuery] = useState<PersonaQuery>({ page: 1, status: "active", scope: "all", q: "" });
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<LoadState<Persona>>(INITIAL_LOAD);

  const [qInput, setQInput] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<Persona | null>(null);
  const [form, setForm] = useState<PersonaFormState>(EMPTY_PERSONA_FORM);
  const [formError, setFormError] = useState<ApiError | null>(null);
  const [formFieldErrors, setFormFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const [confirmTarget, setConfirmTarget] = useState<Persona | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  // 归属账号下拉需要完整账号列表（含已归档，避免历史人设显示为「未知账号」）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const result = await apiClient.get<Page<Account>>(
          `/api/v1/accounts${toQuery({ page: 1, page_size: 100, status: "all" })}`,
        );
        if (!cancelled) setAccounts(result.items);
      } catch {
        if (!cancelled) setAccounts([]);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<Persona>>(
          `/api/v1/personas${toQuery({
            page: query.page,
            page_size: PAGE_SIZE,
            status: query.status,
            account_id: query.scope === "all" ? null : query.scope,
            q: query.q,
          })}`,
        );
        if (!cancelled) setState({ loading: false, data, error: null });
      } catch (err) {
        if (!cancelled) setState({ loading: false, data: null, error: toApiError(err) });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [query, reloadToken]);

  const applyQuery = useCallback((patch: Partial<PersonaQuery>) => {
    setState((prev) => ({ ...prev, loading: true }));
    setQuery((prev) => ({ ...prev, ...patch }));
    setReloadToken((token) => token + 1);
  }, []);

  const reload = useCallback(() => {
    setState((prev) => ({ ...prev, loading: true }));
    setReloadToken((token) => token + 1);
  }, []);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_PERSONA_FORM);
    setFormError(null);
    setFormFieldErrors({});
    setDialogOpen(true);
  };

  const openEdit = (persona: Persona) => {
    setEditing(persona);
    setForm({
      name: persona.name,
      accountId: persona.account_id ?? PUBLIC_SCOPE,
      positioning: persona.positioning ?? "",
      targetAudience: persona.target_audience ?? "",
      tone: persona.tone ?? "",
      contentPillars: persona.content_pillars ?? [],
      taboo: persona.taboo ?? [],
      signatureStyle: persona.signature_style ?? "",
      systemPrompt: persona.system_prompt ?? "",
      isDefault: persona.is_default,
    });
    setFormError(null);
    setFormFieldErrors({});
    setDialogOpen(true);
  };

  const submit = async () => {
    const fieldErrors: Record<string, string> = {};
    const name = form.name.trim();
    if (!name) fieldErrors.name = "名称不能为空";
    else if (name.length > 100) fieldErrors.name = "名称不超过 100 字";
    if (form.tone.trim().length > 200) fieldErrors.tone = "语气风格不超过 200 字";
    setFormFieldErrors(fieldErrors);
    if (Object.keys(fieldErrors).length > 0) return;

    const payload: PersonaWrite = {
      name,
      account_id: form.accountId === PUBLIC_SCOPE ? null : form.accountId,
      positioning: form.positioning.trim() || null,
      target_audience: form.targetAudience.trim() || null,
      tone: form.tone.trim() || null,
      content_pillars: form.contentPillars,
      taboo: form.taboo,
      signature_style: form.signatureStyle.trim() || null,
      system_prompt: form.systemPrompt.trim() || null,
      is_default: form.isDefault,
    };

    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        await apiClient.patch<Persona>(`/api/v1/personas/${editing.id}`, payload);
        setNotice("已更新人设");
      } else {
        await apiClient.post<Persona>("/api/v1/personas", payload);
        setNotice("已新建人设");
      }
      setDialogOpen(false);
      reload();
    } catch (err) {
      setFormError(toApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const changeStatus = async (persona: Persona, next: "active" | "archived") => {
    setConfirmBusy(true);
    setActionError(null);
    try {
      await apiClient.patch<Persona>(`/api/v1/personas/${persona.id}`, { status: next });
      setNotice(next === "archived" ? "已归档人设" : "已恢复人设");
      setConfirmTarget(null);
      reload();
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setConfirmBusy(false);
    }
  };

  const accountOptions = useMemo(
    () =>
      accounts.map((account) => ({
        id: account.id,
        name: account.name + (account.status === "archived" ? "（已归档）" : ""),
      })),
    [accounts],
  );

  const rows = state.data?.items ?? [];

  return (
    <div className="flex flex-col gap-4">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          applyQuery({ q: qInput.trim(), page: 1 });
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="persona-q">关键词</Label>
          <Input
            id="persona-q"
            value={qInput}
            placeholder="按人设名模糊匹配"
            onChange={(event) => setQInput(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="persona-scope">归属</Label>
          <Select
            value={query.scope}
            onValueChange={(value) => applyQuery({ scope: String(value), page: 1 })}
          >
            <SelectTrigger id="persona-scope" className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="public">公共人设库</SelectItem>
              {accountOptions.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="persona-status">状态</Label>
          <Select
            value={query.status}
            onValueChange={(value) => applyQuery({ status: String(value), page: 1 })}
          >
            <SelectTrigger id="persona-status" className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="active">在用</SelectItem>
              <SelectItem value="archived">已归档</SelectItem>
              <SelectItem value="all">全部</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <Button type="submit" variant="outline">
          查询
        </Button>
        <div className="ml-auto">
          <Button type="button" onClick={openCreate}>
            新建人设
          </Button>
        </div>
      </form>

      <ErrorText error={actionError} />
      <Notice text={notice} />

      <Card>
        <CardContent className="pt-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>归属账号</TableHead>
                <TableHead>内容支柱</TableHead>
                <TableHead>默认</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.loading ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : state.error ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center">
                    <ErrorText error={state.error} />
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                    暂无数据（可切换归属 / 状态，或新建人设）
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((persona) => (
                  <TableRow key={persona.id}>
                    <TableCell className="font-medium">{persona.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {persona.account_name ?? "公共人设库"}
                    </TableCell>
                    <TableCell>
                      {persona.content_pillars && persona.content_pillars.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {persona.content_pillars.map((pillar) => (
                            <Badge key={pillar} variant="outline">
                              {pillar}
                            </Badge>
                          ))}
                        </div>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {persona.is_default ? (
                        <Badge className="border-sky-600/30 bg-sky-500/15 text-sky-700">默认</Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={persona.status} />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1.5">
                        <Button variant="outline" size="sm" onClick={() => openEdit(persona)}>
                          编辑
                        </Button>
                        {persona.status === "active" ? (
                          <Button
                            variant="destructive"
                            size="sm"
                            onClick={() => setConfirmTarget(persona)}
                          >
                            归档
                          </Button>
                        ) : (
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => void changeStatus(persona, "active")}
                          >
                            恢复
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <Pagination
            page={query.page}
            pageSize={PAGE_SIZE}
            total={state.data?.total ?? 0}
            loading={state.loading}
            onPage={(next) => applyQuery({ page: next })}
          />
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑人设" : "新建人设"}</DialogTitle>
            <DialogDescription>
              归属选「公共人设库」表示该人设不绑定具体账号，可被任意账号复用。
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-3">
            <Field label="名称 *" htmlFor="persona-name" error={formFieldErrors.name}>
              <Input
                id="persona-name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </Field>

            <Field label="归属账号" htmlFor="persona-account">
              <Select
                value={form.accountId}
                onValueChange={(value) => setForm({ ...form, accountId: String(value) })}
              >
                <SelectTrigger id="persona-account" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={PUBLIC_SCOPE}>公共人设库</SelectItem>
                  {accountOptions.map((option) => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label="定位" htmlFor="persona-positioning" hint="我是谁 / 目标读者 / 价值主张">
              <Textarea
                id="persona-positioning"
                value={form.positioning}
                onChange={(event) => setForm({ ...form, positioning: event.target.value })}
              />
            </Field>
            <Field label="目标读者" htmlFor="persona-audience">
              <Textarea
                id="persona-audience"
                value={form.targetAudience}
                onChange={(event) => setForm({ ...form, targetAudience: event.target.value })}
              />
            </Field>
            <Field label="语气风格" htmlFor="persona-tone" error={formFieldErrors.tone}>
              <Input
                id="persona-tone"
                value={form.tone}
                onChange={(event) => setForm({ ...form, tone: event.target.value })}
              />
            </Field>
            <Field label="内容支柱" hint="最多 10 项，每项 ≤50 字，自动去重">
              <TagInput
                value={form.contentPillars}
                onChange={(next) => setForm({ ...form, contentPillars: next })}
                placeholder="输入后回车或点「添加」"
              />
            </Field>
            <Field label="人设禁忌" hint="最多 10 项，每项 ≤50 字，自动去重">
              <TagInput
                value={form.taboo}
                onChange={(next) => setForm({ ...form, taboo: next })}
                placeholder="输入后回车或点「添加」"
              />
            </Field>
            <Field label="口头禅 / 标志表达" htmlFor="persona-signature">
              <Input
                id="persona-signature"
                value={form.signatureStyle}
                onChange={(event) => setForm({ ...form, signatureStyle: event.target.value })}
              />
            </Field>
            <Field label="人设提示词模板" htmlFor="persona-system-prompt" hint="第 12 步创作 Agent 使用">
              <Textarea
                id="persona-system-prompt"
                className="min-h-24"
                value={form.systemPrompt}
                onChange={(event) => setForm({ ...form, systemPrompt: event.target.value })}
              />
            </Field>

            <div className="flex items-center gap-2">
              <input
                id="persona-is-default"
                type="checkbox"
                className="size-4 rounded border-input accent-primary"
                checked={form.isDefault}
                onChange={(event) => setForm({ ...form, isDefault: event.target.checked })}
              />
              <Label htmlFor="persona-is-default">设为该归属下的默认人设</Label>
            </div>
            <p className="text-xs text-muted-foreground">
              同一归属（账号 / 公共）范围内默认人设唯一，设置后会自动取消其它人设的默认标记。
            </p>

            <ErrorText error={formError} />
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)} disabled={saving}>
              取消
            </Button>
            <Button onClick={() => void submit()} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="确认归档该人设？"
        description={`归档后「${confirmTarget?.name ?? ""}」将不在默认列表显示，可随时恢复。`}
        confirmText="确认归档"
        busy={confirmBusy}
        onCancel={() => setConfirmTarget(null)}
        onConfirm={() => confirmTarget && void changeStatus(confirmTarget, "archived")}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export default function AccountsPage() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">账号与人设</h1>
        <p className="text-sm text-muted-foreground">
          第 8 步：账号 / 人设的全栈 CRUD 范式。浏览器只请求本站 /api/v1/**，由服务端
          Route Handler 转发到后端；本工具不登录小红书、不保存任何登录态。
        </p>
        <div>
          <Link href="/" className="text-sm text-primary underline-offset-4 hover:underline">
            ← 返回环境自检
          </Link>
        </div>
      </header>

      <Tabs defaultValue="accounts">
        <TabsList>
          <TabsTrigger value="accounts">账号</TabsTrigger>
          <TabsTrigger value="personas">人设</TabsTrigger>
        </TabsList>
        <TabsContent value="accounts">
          <AccountsTab />
        </TabsContent>
        <TabsContent value="personas">
          <PersonasTab />
        </TabsContent>
      </Tabs>
    </main>
  );
}
