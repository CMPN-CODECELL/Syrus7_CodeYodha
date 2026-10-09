"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import {
  CircleCheck,
  CircleUserRound,
  CircleX,
  FileLock,
  HeartPulse,
  KeyRound,
  LaptopMinimal,
  LockKeyhole,
  Monitor,
  Plus,
  Power,
  ScrollText,
  ShieldAlert,
  ShieldCheck,
  Signature,
  Smartphone,
  Trash,
} from "lucide-react";
import { toast } from "sonner";
import { PinResetInline, PinSlots, RegisterPasskeyInline, SetPinInline, TotpSetupInline } from "@/components/approval/setup-inline";
import { BrokerConnectionPanel } from "@/components/broker/broker-connection";
import { Button } from "@/components/ui/button";
import { Chip, Dot } from "@/components/ui/chip";
import { TableShell, Td, Th } from "@/components/ui/data-table";
import { Hash } from "@/components/ui/hash";
import { PageContainer, PageHeader, Panel } from "@/components/ui/panel";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/ui/states";
import { api } from "@/lib/api";
import { accountId, activePasskeys } from "@/lib/auth";
import { brokerName, displayAccountId } from "@/lib/broker";
import { errorMessage } from "@/lib/errors";
import { ago, dateTime, shortHash, time } from "@/lib/format";
import {
  qk,
  useActiveBroker,
  useAudit,
  useKillSwitch,
  useMe,
  useProposals,
  useSecurityEvents,
  useSecurityStatus,
  useSessions,
} from "@/lib/queries";
import { proposalLook } from "@/lib/status";
import { useLiveStore } from "@/lib/store";
import type { AuditVerify, Me, SecurityEventScope, SecurityStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

// ---------------------------------------------------------------------------

function TrustFact({ label, value, good, detail }: { label: string; value: string; good: boolean; detail: string }) {
  return (
    <div className={cn("rounded-lg p-3 ring-1", good ? "bg-profit/[0.05] ring-profit/25" : "bg-danger/[0.07] ring-danger/40")}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs leading-snug text-muted-foreground">{label}</p>
        {good ? <CircleCheck className="size-4 shrink-0 text-profit" /> : <CircleX className="size-4 shrink-0 text-danger" />}
      </div>
      <p className={cn("num mt-1 text-2xl font-semibold tracking-tight", good ? "text-profit" : "text-danger")}>{value}</p>
      <p className="mt-0.5 text-[11px] text-subtle">{detail}</p>
    </div>
  );
}

function TrustBoundary({ s }: { s: SecurityStatus }) {
  return (
    <Panel title="Trust boundary" icon={ShieldCheck} subtitle="Who can hold the broker write key, verified at runtime">
      <div className="grid gap-2 p-3 sm:grid-cols-3">
        <TrustFact label="API process holds broker write key" value={s.api.holds_write_key ? "YES" : "NO"} good={!s.api.holds_write_key} detail="The web API only has the READ key" />
        <TrustFact label="AI has order tools" value={s.api.llm_has_order_tools ? "YES" : "NO"} good={!s.api.llm_has_order_tools} detail="Model tools are read-only plus draft" />
        <TrustFact
          label="Executor is the only holder of write credentials"
          value={s.executor.holds_write_key && !s.api.holds_write_key ? "YES" : "NO"}
          good={s.executor.holds_write_key && !s.api.holds_write_key}
          detail="Separate process, no LLM libraries"
        />
      </div>
    </Panel>
  );
}

function KillSwitchPanel({ s }: { s: SecurityStatus }) {
  const kill = useKillSwitch();
  const [pin, setPin] = useState("");
  const engaged = s.kill_switch.engaged;
  return (
    <Panel
      id="kill-switch"
      title="Kill switch"
      icon={Power}
      subtitle={engaged ? "Engaged: everything is halted" : "Halts every approval and execution instantly"}
      className={cn(engaged && "ring-2 ring-danger/50")}
    >
      <div className="space-y-3 p-4">
        {engaged ? (
          <>
            <div className="flex items-start gap-2 rounded-lg bg-danger/10 p-3 text-sm ring-1 ring-danger/35">
              <Power className="mt-0.5 size-4 shrink-0 animate-pulse text-danger" />
              <div>
                <p className="font-semibold text-danger">Engaged{s.kill_switch.at ? ` at ${time(s.kill_switch.at)}` : ""}</p>
                {s.kill_switch.reason && <p className="text-xs text-muted-foreground">{s.kill_switch.reason}</p>}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">Disengaging requires your 4-digit app PIN.</p>
            <PinSlots value={pin} onChange={setPin} length={4} masked label="PIN to disengage" invalid={!!kill.error} />
            <Button
              className="w-full"
              variant="outline"
              disabled={pin.length !== 4 || kill.isPending}
              onClick={() =>
                kill.mutate(
                  { engaged: false, pin },
                  {
                    onSuccess: () => {
                      setPin("");
                      toast.success("Kill switch disengaged", { description: "Executions may resume. Pending approvals still need fresh signatures." });
                    },
                    onError: () => setPin(""),
                  },
                )
              }
            >
              {kill.isPending ? "Verifying PIN..." : "Disengage with PIN"}
            </Button>
          </>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Engaging is instant and needs no confirmation. The executor stops consuming capabilities and every pending approval fails closed.
            </p>
            <Button
              size="lg"
              className="h-12 w-full bg-danger text-[15px] font-semibold text-white hover:bg-danger/90"
              disabled={kill.isPending}
              onClick={() =>
                kill.mutate(
                  { engaged: true },
                  { onSuccess: () => toast.error("Kill switch engaged", { description: "All approvals and executions are halted." }) },
                )
              }
            >
              <Power /> Engage kill switch
            </Button>
          </>
        )}
        {kill.error && <ErrorState error={kill.error} title={engaged ? "Kill switch is still engaged" : "Kill switch did not engage"} compact />}
      </div>
    </Panel>
  );
}

function MonitorPanel({ s }: { s: SecurityStatus }) {
  const live = useLiveStore((st) => st.monitor);
  const m = live ?? s.monitor;
  const now = useNow(1000);
  const hb = m.last_heartbeat ? ago(m.last_heartbeat, now || Date.parse(m.last_heartbeat)) : "never";
  return (
    <Panel
      title="Invariant monitor"
      icon={HeartPulse}
      subtitle="Independent read-only process that checks the guarantees continuously"
      actions={
        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Dot tone={m.healthy ? "profit" : "danger"} pulse />
          heartbeat {hb}
        </span>
      }
    >
      <div className="grid grid-cols-2 gap-2 border-b border-hairline p-3">
        <div className="rounded-lg bg-surface-2/60 p-2.5 ring-1 ring-hairline">
          <div className={cn("num text-2xl font-semibold", m.unapproved_executions ? "text-danger" : "text-profit")}>{m.unapproved_executions}</div>
          <div className="text-[11px] text-muted-foreground">Unapproved executions</div>
        </div>
        <div className="rounded-lg bg-surface-2/60 p-2.5 ring-1 ring-hairline">
          <div className={cn("num text-2xl font-semibold", m.violations ? "text-danger" : "text-profit")}>{m.violations}</div>
          <div className="text-[11px] text-muted-foreground">Invariant violations</div>
        </div>
      </div>
      <ul className="divide-y divide-hairline">
        {m.checks.map((c) => (
          <li key={c.name} className="flex items-start gap-2.5 px-4 py-2.5">
            {c.ok ? <CircleCheck className="mt-0.5 size-4 shrink-0 text-profit" /> : <CircleX className="mt-0.5 size-4 shrink-0 text-danger" />}
            <div className="min-w-0 flex-1">
              <p className="text-[13px] leading-snug">{c.name}</p>
              <p className="text-[11px] text-subtle">{c.detail}</p>
            </div>
            <span className="num shrink-0 text-xs text-muted-foreground" title="Times checked">
              {c.checked.toLocaleString("en-IN")}
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function SecurityEventsPanel() {
  const [scope, setScope] = useState<SecurityEventScope>("mine");
  const { data, error, isPending, refetch } = useSecurityEvents(scope);
  return (
    <Panel
      title="Security events"
      icon={ShieldAlert}
      subtitle={scope === "mine" ? "Your account and system-wide alerts" : "Including Attack Lab drills on the shared sandbox"}
      actions={
        <div className="flex rounded-md bg-surface-2/70 p-0.5 ring-1 ring-hairline" role="radiogroup" aria-label="Which events">
          {(["mine", "all"] as const).map((v) => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={scope === v}
              onClick={() => setScope(v)}
              className={cn(
                "min-h-7 rounded px-2 text-[11px] font-medium transition-colors",
                scope === v ? "bg-background text-foreground shadow-sm ring-1 ring-hairline-strong" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {v === "mine" ? "Yours" : "With drills"}
            </button>
          ))}
        </div>
      }
    >
      {isPending ? (
        <SkeletonRows rows={5} />
      ) : error ? (
        <ErrorState error={error} title="Security events unavailable" onRetry={() => void refetch()} className="m-3" />
      ) : data.length === 0 ? (
        <EmptyState
          icon={ShieldCheck}
          title="No security events"
          description={scope === "mine" ? "Nothing has happened on your account. Attack Lab drills are under With drills." : undefined}
        />
      ) : (
        <TableShell maxHeight="380px">
          <thead>
            <tr>
              <Th>Time</Th>
              <Th>Severity</Th>
              <Th>Event</Th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id} className="align-top">
                <Td num className="text-xs text-muted-foreground">
                  {dateTime(e.ts)}
                </Td>
                <Td>
                  {e.scope === "drill" ? (
                    <Chip tone="muted" size="xs">
                      drill
                    </Chip>
                  ) : (
                    <Chip tone={e.severity === "critical" ? "danger" : e.severity === "warning" ? "warning" : "info"} size="xs">
                      {e.severity}
                    </Chip>
                  )}
                </Td>
                <Td className="whitespace-normal">
                  <p className="font-mono text-[11px] text-muted-foreground">
                    {e.kind}
                    {e.scope === "drill" ? (
                      <span className="ml-1.5 font-sans text-subtle">{e.resolved ? "Blocked (drill)" : "Detected (drill), being contained"}</span>
                    ) : (
                      !e.resolved && <span className="ml-1.5 text-warning">open</span>
                    )}
                  </p>
                  <p className="text-[12.5px] leading-snug">{e.message}</p>
                </Td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------

function AccountRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3 py-2">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right text-[13px]">{children}</dd>
    </div>
  );
}

function AccountPanel({ me }: { me: Me }) {
  const broker = useActiveBroker();
  const methods = [
    me.has_pin ? "App PIN" : null,
    activePasskeys(me) ? `${activePasskeys(me)} passkey${activePasskeys(me) === 1 ? "" : "s"}` : null,
    me.has_totp ? "Authenticator app" : null,
  ].filter(Boolean);
  return (
    <Panel title="Your account" icon={CircleUserRound} subtitle="Signing in is not approval; every trade needs a fresh countersignature">
      <dl className="divide-y divide-hairline px-4 py-1">
        <AccountRow label="Name">{me.user.display_name}</AccountRow>
        <AccountRow label="Email">{me.email ?? <span className="text-subtle">not on file</span>}</AccountRow>
        <AccountRow label="Trading account">
          <span className="num font-medium">{displayAccountId(broker.account?.account_id ?? accountId(me), broker.kind)}</span>
          <span className="ml-1.5 text-xs text-subtle">{brokerName(broker.kind)}</span>
        </AccountRow>
        <AccountRow label="Countersign with">{methods.length ? methods.join(", ") : <span className="text-warning">no method set up yet</span>}</AccountRow>
        <AccountRow label="Session expires">
          <span className="num">{dateTime(me.session.expires_at)}</span>
        </AccountRow>
      </dl>
    </Panel>
  );
}

function DevicesPanel({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeDevice(id),
    onSuccess: () => {
      toast("Passkey revoked", { description: "That device can no longer countersign." });
      void qc.invalidateQueries({ queryKey: qk.me });
    },
    onError: (e) => toast.error("Could not revoke the passkey", { description: errorMessage(e) }),
  });
  return (
    <Panel
      title="Passkeys"
      icon={KeyRound}
      subtitle="Device-signed countersignatures (WebAuthn)"
      actions={
        <Button size="sm" variant="outline" onClick={() => setAdding((v) => !v)}>
          <Plus /> Register
        </Button>
      }
    >
      <div className="space-y-3 p-3">
        {adding && <RegisterPasskeyInline onDone={() => setAdding(false)} />}
        {me.devices.length === 0 ? (
          <EmptyState icon={KeyRound} title="No passkeys yet" description="Register this computer to countersign with Windows Hello or Touch ID." className="py-6" />
        ) : (
          <ul className="space-y-2">
            {me.devices.map((d) => (
              <li key={d.id} className={cn("flex items-center gap-3 rounded-lg bg-surface-2/50 px-3 py-2.5 ring-1 ring-hairline", d.revoked && "opacity-60")}>
                {d.transports.includes("internal") ? <LaptopMinimal className="size-4 shrink-0 text-muted-foreground" /> : <Smartphone className="size-4 shrink-0 text-muted-foreground" />}
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 truncate text-[13px] font-medium">
                    {d.label}
                    {d.is_virtual && (
                      <Chip tone="warning" size="xs">
                        virtual
                      </Chip>
                    )}
                    {d.revoked && (
                      <Chip tone="muted" size="xs">
                        revoked
                      </Chip>
                    )}
                  </p>
                  <p className="num truncate text-[11px] text-subtle">
                    added {dateTime(d.created_at)} · last used {d.last_used_at ? dateTime(d.last_used_at) : "never"}
                    {d.transports.length ? ` · ${d.transports.join(", ")}` : ""}
                  </p>
                </div>
                {!d.revoked && (
                  <Button
                    size="sm"
                    variant={confirm === d.id ? "destructive" : "ghost"}
                    disabled={revoke.isPending}
                    onClick={() => {
                      if (confirm !== d.id) {
                        setConfirm(d.id);
                        setTimeout(() => setConfirm((c) => (c === d.id ? null : c)), 3500);
                        return;
                      }
                      setConfirm(null);
                      revoke.mutate(d.id);
                    }}
                  >
                    <Trash /> {confirm === d.id ? "Confirm" : "Revoke"}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function PinPanel({ me }: { me: Me }) {
  const [resetting, setResetting] = useState(false);
  return (
    <Panel
      title="App PIN"
      icon={LockKeyhole}
      subtitle="4-digit fallback, server-attested"
      actions={
        <Chip tone={me.pin_locked_until ? "danger" : me.has_pin ? "profit" : "muted"} size="xs">
          {me.pin_locked_until ? "locked" : me.has_pin ? "set" : "not set"}
        </Chip>
      }
    >
      <div className="space-y-3 p-4">
        {me.pin_locked_until && <p className="text-xs text-danger">Locked until {dateTime(me.pin_locked_until)} after too many incorrect attempts.</p>}
        {!me.has_pin && (
          <p className="text-sm text-muted-foreground">
            You have not chosen an app PIN yet. It is a fallback way to countersign and the only way to disengage the kill switch.
          </p>
        )}
        {resetting ? (
          <>
            <p className="text-sm text-muted-foreground">
              Confirm it is you with your account password
              {me.has_totp ? " and a code from your authenticator app" : activePasskeys(me) ? " and your passkey" : ""}, then choose a new PIN.
              This also clears a PIN lockout and signs out your other sessions.
            </p>
            <PinResetInline hasTotp={me.has_totp} hasPasskey={activePasskeys(me) > 0} onDone={() => setResetting(false)} />
            <Button variant="ghost" size="sm" onClick={() => setResetting(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            <SetPinInline hasPin={me.has_pin} />
            {me.has_pin && (
              <button
                type="button"
                onClick={() => setResetting(true)}
                className="text-xs font-medium text-human underline-offset-4 hover:underline focus-visible:underline"
              >
                Forgot or locked PIN? Reset it
              </button>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

function TotpPanel({ me }: { me: Me }) {
  return (
    <Panel
      id="totp"
      title="Authenticator app"
      icon={Smartphone}
      subtitle="6-digit TOTP fallback"
      actions={
        <Chip tone={me.has_totp ? "profit" : "muted"} size="xs">
          {me.has_totp ? "enabled" : "off"}
        </Chip>
      }
    >
      <div className="space-y-3 p-4">
        {me.has_totp ? (
          <p className="text-sm text-muted-foreground">An authenticator app is linked. Codes rotate every 30 s and are checked server-side.</p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">Link an authenticator app (any TOTP app) as a second fallback to your passkey.</p>
            <TotpSetupInline />
          </>
        )}
      </div>
    </Panel>
  );
}

function SessionsPanel() {
  const qc = useQueryClient();
  const { data, error, isPending, refetch } = useSessions();
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeSession(id),
    onSuccess: () => {
      toast("Session revoked");
      void qc.invalidateQueries({ queryKey: qk.sessions });
    },
    onError: (e) => toast.error("Could not revoke the session", { description: errorMessage(e) }),
  });
  return (
    <Panel title="Active sessions" icon={Monitor} subtitle="Signing in is not approval; sessions cannot countersign">
      {isPending ? (
        <SkeletonRows rows={3} />
      ) : error ? (
        <ErrorState error={error} title="Sessions unavailable" onRetry={() => void refetch()} className="m-3" />
      ) : (
        <ul className="divide-y divide-hairline">
          {data.map((s) => (
            <li key={s.id} className={cn("flex items-center gap-3 px-4 py-2.5", s.revoked && "opacity-55")}>
              <Monitor className="size-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-1.5 truncate text-[13px]">
                  {s.user_agent}
                  {s.current && (
                    <Chip tone="human" size="xs">
                      this session
                    </Chip>
                  )}
                  {s.revoked && (
                    <Chip tone="muted" size="xs">
                      revoked
                    </Chip>
                  )}
                </p>
                <p className="num text-[11px] text-subtle">
                  started {dateTime(s.created_at)} · expires {dateTime(s.expires_at)}
                </p>
              </div>
              {!s.current && !s.revoked && (
                <Button size="sm" variant="ghost" disabled={revoke.isPending} onClick={() => revoke.mutate(s.id)}>
                  Revoke
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ApprovalHistoryPanel() {
  const { data, error, isPending, refetch } = useProposals({ limit: 100 });
  const rows = (data ?? []).filter((p) => p.approval);
  return (
    <Panel title="Approval history" icon={Signature} subtitle="Every countersignature, with method and assurance">
      {isPending ? (
        <SkeletonRows rows={4} />
      ) : error ? (
        <ErrorState error={error} title="Approval history unavailable" onRetry={() => void refetch()} className="m-3" />
      ) : rows.length === 0 ? (
        <EmptyState icon={Signature} title="No countersignatures yet" description="Approvals you sign appear here with their payload hash." />
      ) : (
        <TableShell maxHeight="360px">
          <thead>
            <tr>
              <Th>Time</Th>
              <Th>Proposal</Th>
              <Th>Method</Th>
              <Th>Approval</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const a = p.approval!;
              return (
                <tr key={p.id} className="align-top">
                  <Td num className="text-xs text-muted-foreground">
                    {dateTime(a.verified_at ?? a.issued_at)}
                  </Td>
                  <Td className="whitespace-normal">
                    <p className="num text-[12px] font-medium">{p.short_hash}</p>
                    <p className="line-clamp-1 max-w-[260px] text-[11px] text-subtle">{p.summary}</p>
                  </Td>
                  <Td>
                    <p className="text-[12px]">{a.method === "webauthn" ? "Passkey" : a.method === "pin" ? "App PIN" : "Authenticator"}</p>
                    <p className="text-[11px] text-subtle">{a.assurance === "device_signed" ? "device-signed" : "server-attested"}</p>
                  </Td>
                  <Td>
                    <Chip tone={a.status === "VERIFIED" ? "profit" : a.status === "CHALLENGED" ? "info" : "danger"} size="xs">
                      {a.status}
                    </Chip>
                    <p className="num mt-1 text-[10.5px] text-subtle">{shortHash(a.payload_hash, 12)}</p>
                  </Td>
                  <Td>
                    <Chip tone={proposalLook(p.status).tone} size="xs">
                      {proposalLook(p.status).label}
                    </Chip>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </TableShell>
      )}
    </Panel>
  );
}

function AuditPanel() {
  const { data, error, isPending, refetch } = useAudit();
  const [result, setResult] = useState<AuditVerify | null>(null);
  const verify = useMutation({
    mutationFn: api.auditVerify,
    onSuccess: (r) => setResult(r),
  });
  return (
    <Panel
      title="Audit log"
      icon={ScrollText}
      subtitle="Append-only, hash-chained: each event commits to the previous event's hash"
      actions={
        <Button size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
          <FileLock /> {verify.isPending ? "Verifying chain..." : "Verify audit chain"}
        </Button>
      }
    >
      {result && (
        <div
          role="status"
          className={cn(
            "m-3 flex items-start gap-2 rounded-lg p-3 text-sm ring-1",
            result.ok ? "bg-profit/[0.07] text-profit ring-profit/30" : "bg-danger/10 text-danger ring-danger/40",
          )}
        >
          {result.ok ? <CircleCheck className="mt-0.5 size-4 shrink-0" /> : <CircleX className="mt-0.5 size-4 shrink-0" />}
          <div className="min-w-0">
            <p className="font-semibold">
              {result.ok
                ? `Chain intact: ${result.checked.toLocaleString("en-IN")} events verified, head hash ${shortHash(result.head_hash, 16)}`
                : `Tampering detected at event #${result.broken_at_id}`}
            </p>
            <p className="text-xs text-muted-foreground">{result.detail}</p>
          </div>
        </div>
      )}
      {verify.error && <ErrorState error={verify.error} title="Chain verification did not run" className="m-3" compact />}
      {isPending ? (
        <SkeletonRows rows={8} />
      ) : error ? (
        <ErrorState error={error} title="Audit log unavailable" onRetry={() => void refetch()} className="m-3" />
      ) : (
        <TableShell maxHeight="520px">
          <thead>
            <tr>
              <Th align="right">#</Th>
              <Th>Time</Th>
              <Th>Actor</Th>
              <Th>Event</Th>
              <Th>Subject</Th>
              <Th>Hash</Th>
              <Th>Prev hash</Th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id} className={cn("hover:bg-foreground/[0.03]", result && !result.ok && result.broken_at_id === e.id && "bg-danger/10")}>
                <Td align="right" num className="text-muted-foreground">
                  {e.id}
                </Td>
                <Td num className="text-xs text-muted-foreground">
                  {dateTime(e.ts)}
                </Td>
                <Td className="font-mono text-[11.5px]">{e.actor}</Td>
                <Td className="font-mono text-[11.5px] font-medium">{e.event_type}</Td>
                <Td className="font-mono text-[11px] text-muted-foreground">
                  {e.subject_type}:{e.subject_id}
                </Td>
                <Td>
                  <Hash value={e.hash} display={shortHash(e.hash, 10)} className="text-[11px]" label="event hash" />
                </Td>
                <Td className="font-mono text-[11px] text-subtle">{shortHash(e.prev_hash, 10)}</Td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------

export function SecurityView() {
  const status = useSecurityStatus();
  const me = useMe();
  return (
    <PageContainer wide>
      <PageHeader
        icon={ShieldCheck}
        title="Security Center"
        description="Your broker connection, countersign methods, the kill switch, the independent monitor and the tamper-evident audit chain."
      />
      {status.error ? (
        <ErrorState error={status.error} title="Security status unavailable" onRetry={() => void status.refetch()} />
      ) : !status.data ? (
        <SkeletonRows rows={6} />
      ) : (
        <>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <TrustBoundary s={status.data} />
            <KillSwitchPanel s={status.data} />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <MonitorPanel s={status.data} />
            <SecurityEventsPanel />
          </div>
        </>
      )}
      {me.data && <BrokerConnectionPanel me={me.data} />}
      {me.data && (
        <div className="grid gap-4 lg:grid-cols-2">
          <AccountPanel me={me.data} />
          <DevicesPanel me={me.data} />
          <PinPanel me={me.data} />
          <TotpPanel me={me.data} />
        </div>
      )}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <SessionsPanel />
        <ApprovalHistoryPanel />
      </div>
      <AuditPanel />
    </PageContainer>
  );
}
