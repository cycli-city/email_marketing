"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { createApiClient, type ApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type Slot = components["schemas"]["ProfileSlot"];
type Mode = Slot["mode"];
type Policy = components["schemas"]["PolicyOut"];
type PolicyPatch = components["schemas"]["PolicyPatch"];
type Consent = Policy["cold_b2b_marketing"];

interface ApiError {
  error?: { message?: string; details?: { field?: string; loosens?: string[] } | null };
}

const errorOf = (error: unknown): ApiError["error"] => (error as ApiError | undefined)?.error;

const MODES: { mode: Mode; title: string; blurb: string }[] = [
  {
    mode: "nexora",
    title: "NEXORA Sales",
    blurb:
      "Every sales e-mail names you and carries your postal address and an unsubscribe link. Most countries' rules require all three.",
  },
  {
    mode: "job_hunter",
    title: "Job Hunter",
    blurb: "Job applications only need your name; there is no marketing footer.",
  },
];

const CONSENT_LABEL: Record<Consent, string> = {
  allowed: "Allowed, with an opt-out",
  requires_consent: "Only with recorded consent",
  blocked: "Never",
};
const CONSENT_ORDER: Consent[] = ["allowed", "requires_consent", "blocked"];

// ───────────────────────────── sender identity ─────────────────────────────

interface Draft {
  display_name: string;
  postal_address: string;
  reply_to: string;
  website: string;
  signature_text: string;
}

const draftOf = (slot: Slot): Draft => ({
  display_name: slot.profile?.display_name ?? "",
  postal_address: slot.profile?.postal_address ?? "",
  reply_to: slot.profile?.reply_to ?? "",
  website: slot.profile?.website ?? "",
  signature_text: slot.profile?.signature_text ?? "",
});

function IdentityCard({
  slot,
  title,
  blurb,
  client,
}: {
  slot: Slot;
  title: string;
  blurb: string;
  client: ApiClient;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(() => draftOf(slot));
  const [fieldError, setFieldError] = useState<{ field: string; text: string } | null>(null);
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await client.PUT("/api/v1/settings/sender-profiles/{mode}", {
        params: { path: { mode: slot.mode } },
        body: {
          display_name: draft.display_name,
          postal_address: draft.postal_address || null,
          reply_to: draft.reply_to || null,
          website: draft.website || null,
          signature_text: draft.signature_text || null,
        },
      });
      if (!data) {
        const problem = errorOf(error);
        setFieldError({
          field: problem?.details?.field ?? "",
          text: problem?.message ?? "Could not save the sender identity.",
        });
        throw new Error("rejected");
      }
      return data;
    },
    onSuccess: (stored) => {
      setFieldError(null);
      setDraft(draftOf(stored)); // show exactly what the server kept (it trims and normalises), without remounting
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ["sender-profiles"] });
    },
  });

  const set = (name: keyof Draft) => (value: string) => {
    setDraft((current) => ({ ...current, [name]: value }));
    setSaved(false);
  };
  const errorFor = (field: keyof Draft) => (fieldError?.field === field ? fieldError.text : null);
  const ready = slot.missing.length === 0;

  const text = (name: keyof Draft, label: string, hint?: string) => (
    <div className="space-y-1.5">
      <Label htmlFor={`${slot.mode}-${name}`}>{label}</Label>
      <Input
        id={`${slot.mode}-${name}`}
        value={draft[name]}
        onChange={(event) => set(name)(event.target.value)}
        aria-invalid={errorFor(name) ? true : undefined}
        aria-describedby={errorFor(name) ? `${slot.mode}-${name}-error` : undefined}
      />
      {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
      {errorFor(name) && (
        <p id={`${slot.mode}-${name}-error`} className="text-destructive text-xs" role="alert">
          {errorFor(name)}
        </p>
      )}
    </div>
  );

  return (
    <Card aria-label={title}>
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle className="text-base">{title}</CardTitle>
          <CardDescription>{blurb}</CardDescription>
        </div>
        {ready ? (
          <Badge variant="secondary" className="gap-1">
            <CheckCircle2 className="size-3 text-emerald-600 dark:text-emerald-400" />
            Ready to send
          </Badge>
        ) : (
          <Badge variant="destructive" className="gap-1">
            <TriangleAlert className="size-3" />
            Not ready
          </Badge>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {!ready && (
          <p className="text-sm" role="status">
            Still needed before this mode can send: {slot.missing.join(", ")}.
          </p>
        )}
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            {text("display_name", "Your name as it appears on the e-mail")}
            {text(
              "reply_to",
              "Reply-to address (optional)",
              "Replies go here instead of the sending mailbox.",
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${slot.mode}-postal_address`}>Postal address</Label>
            <Textarea
              id={`${slot.mode}-postal_address`}
              rows={3}
              value={draft.postal_address}
              onChange={(event) => set("postal_address")(event.target.value)}
              aria-invalid={errorFor("postal_address") ? true : undefined}
            />
            <p className="text-muted-foreground text-xs">
              {slot.mode === "nexora"
                ? "Shown in the footer of every sales e-mail. Required by most countries' rules."
                : "Optional for job applications."}
            </p>
            {errorFor("postal_address") && (
              <p className="text-destructive text-xs" role="alert">
                {errorFor("postal_address")}
              </p>
            )}
          </div>
          <div className="grid gap-4 sm:grid-cols-2">{text("website", "Website (optional)")}</div>
          <div className="space-y-1.5">
            <Label htmlFor={`${slot.mode}-signature_text`}>Signature (optional)</Label>
            <Textarea
              id={`${slot.mode}-signature_text`}
              rows={3}
              value={draft.signature_text}
              onChange={(event) => set("signature_text")(event.target.value)}
            />
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={save.isPending} aria-label={`Save the ${title} sender identity`}>
              Save
            </Button>
            {saved && (
              <span role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
                Saved
              </span>
            )}
            {fieldError && !fieldError.field && (
              <span role="alert" className="text-destructive text-sm">
                {fieldError.text}
              </span>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ───────────────────────────── country policies ─────────────────────────────

interface Pending {
  country: string;
  patch: PolicyPatch;
  loosens: string[];
}

const LOOSENS_LABEL: Record<string, string> = {
  cold_b2b_marketing: "cold e-mail to companies",
  cold_to_individuals: "cold e-mail to individuals",
  requires_unsubscribe: "the unsubscribe requirement",
  requires_postal_address: "the postal-address requirement",
};

function PolicyRow({
  policy,
  onSave,
  busy,
}: {
  policy: Policy;
  onSave: (country: string, patch: PolicyPatch) => void;
  busy: boolean;
}) {
  const [b2b, setB2b] = useState<Consent>(policy.cold_b2b_marketing);
  const [people, setPeople] = useState<Consent>(policy.cold_to_individuals);
  const [unsubscribe, setUnsubscribe] = useState(policy.requires_unsubscribe);
  const [postal, setPostal] = useState(policy.requires_postal_address);
  const label = policy.country === "ZZ" ? "Everywhere else" : policy.country;

  const select = (id: string, value: Consent, onChange: (next: Consent) => void, name: string) => (
    <select
      id={id}
      aria-label={`${name} for ${label}`}
      value={value}
      onChange={(event) => onChange(event.target.value as Consent)}
      className="border-input bg-background h-8 w-full rounded-md border px-2 text-sm"
    >
      {CONSENT_ORDER.map((option) => (
        <option key={option} value={option}>
          {CONSENT_LABEL[option]}
        </option>
      ))}
    </select>
  );

  return (
    <tr className="border-t align-top">
      <th scope="row" className="py-2 pr-3 text-left font-medium">
        {label}
      </th>
      <td className="py-2 pr-3">{select(`b2b-${policy.country}`, b2b, setB2b, "Companies")}</td>
      <td className="py-2 pr-3">{select(`people-${policy.country}`, people, setPeople, "Individuals")}</td>
      <td className="py-2 pr-3">
        <label className="flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={unsubscribe}
            onChange={(event) => setUnsubscribe(event.target.checked)}
            aria-label={`Require an unsubscribe link for ${label}`}
          />
          Unsubscribe link
        </label>
        <label className="mt-1 flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={postal}
            onChange={(event) => setPostal(event.target.checked)}
            aria-label={`Require a postal address for ${label}`}
          />
          Postal address
        </label>
      </td>
      <td className="py-2 pr-3 text-xs">
        {policy.reviewed_on ? (
          <span>Reviewed {policy.reviewed_on}</span>
        ) : (
          <Badge variant="outline">Default, not reviewed</Badge>
        )}
      </td>
      <td className="py-2">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          aria-label={`Save and mark ${label} reviewed`}
          onClick={() =>
            onSave(policy.country, {
              cold_b2b_marketing: b2b,
              cold_to_individuals: people,
              requires_unsubscribe: unsubscribe,
              requires_postal_address: postal,
              mark_reviewed: true,
              acknowledge_loosening: false,
            })
          }
        >
          Save
        </Button>
      </td>
    </tr>
  );
}

function PolicyTable({ client }: { client: ApiClient }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [onlyUnreviewed, setOnlyUnreviewed] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const policies = useQuery({
    queryKey: ["policies"],
    queryFn: async () => {
      const { data, response } = await client.GET("/api/v1/settings/policies");
      if (!data) throw new Error(`Could not load the policies (HTTP ${response.status})`);
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async ({ country, patch }: { country: string; patch: PolicyPatch }) => {
      const { data, error } = await client.PATCH("/api/v1/settings/policies/{country}", {
        params: { path: { country } },
        body: patch,
      });
      if (data) return { kind: "saved" as const, country };
      const problem = errorOf(error);
      const loosens = problem?.details?.loosens;
      if (loosens?.length) return { kind: "needs_ack" as const, country, patch, loosens };
      throw new Error(problem?.message ?? "Could not save the policy.");
    },
    onSuccess: (result) => {
      if (result.kind === "needs_ack") {
        setPending({ country: result.country, patch: result.patch, loosens: result.loosens });
        return;
      }
      setPending(null);
      setNotice(`Saved the policy for ${result.country === "ZZ" ? "everywhere else" : result.country}.`);
      void queryClient.invalidateQueries({ queryKey: ["policies"] });
    },
    onError: (error) => setNotice(error.message),
  });

  const visible = useMemo(() => {
    const wanted = filter.trim().toUpperCase();
    return (policies.data?.policies ?? []).filter(
      (p) => (!wanted || p.country.includes(wanted)) && (!onlyUnreviewed || !p.reviewed_on),
    );
  }, [policies.data, filter, onlyUnreviewed]);

  if (policies.isPending) return <Skeleton className="h-64 w-full" aria-label="Loading policies" />;
  if (policies.isError) {
    return (
      <p className="text-destructive text-sm" role="alert">
        Could not load the country policies. Check that the backend is running, then refresh.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm" role="status">
        {policies.data.unreviewed === 0
          ? "Every country's policy has been reviewed."
          : `${policies.data.unreviewed} of ${policies.data.policies.length} countries still use the built-in default and have not been reviewed.`}
      </p>
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <Label htmlFor="policy-filter" className="text-sm">
            Country
          </Label>
          <Input
            id="policy-filter"
            className="h-8 w-28"
            placeholder="e.g. DE"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={onlyUnreviewed}
            onChange={(event) => setOnlyUnreviewed(event.target.checked)}
          />
          Only countries I have not reviewed
        </label>
      </div>
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground text-left text-xs">
              <th className="pb-2 pr-3 font-medium">Country</th>
              <th className="pb-2 pr-3 font-medium">Cold e-mail to companies</th>
              <th className="pb-2 pr-3 font-medium">Cold e-mail to individuals</th>
              <th className="pb-2 pr-3 font-medium">Every message needs</th>
              <th className="pb-2 pr-3 font-medium">Status</th>
              <th className="pb-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {visible.map((policy) => (
              <PolicyRow
                // Remount when the stored policy changes so the draft always starts from what is saved.
                key={`${policy.country}-${policy.version}`}
                policy={policy}
                busy={save.isPending}
                onSave={(country, patch) => save.mutate({ country, patch })}
              />
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <p className="text-muted-foreground py-4 text-sm">No country matches.</p>}
      </div>

      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>This makes sending easier</AlertDialogTitle>
            <AlertDialogDescription>
              You are relaxing {pending?.loosens.map((key) => LOOSENS_LABEL[key] ?? key).join(" and ")} for{" "}
              {pending?.country === "ZZ" ? "everywhere else" : pending?.country}. The built-in defaults are
              deliberately cautious and are not legal advice; you are responsible for the rules that apply to
              you. The change is recorded in the audit log.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it as it was</AlertDialogCancel>
            <AlertDialogAction
              onClick={() =>
                pending &&
                save.mutate({
                  country: pending.country,
                  patch: { ...pending.patch, acknowledge_loosening: true },
                })
              }
            >
              I understand, relax it
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// ───────────────────────────── the page body ─────────────────────────────

export function SendingSettings({ api }: { api?: ApiClient }) {
  const client = useMemo(() => api ?? createApiClient(), [api]);

  const profiles = useQuery({
    queryKey: ["sender-profiles"],
    queryFn: async () => {
      const { data, response } = await client.GET("/api/v1/settings/sender-profiles");
      if (!data) throw new Error(`Could not load the sender identity (HTTP ${response.status})`);
      return data.profiles;
    },
  });

  return (
    <div className="space-y-10">
      <section className="space-y-4" aria-labelledby="identity-heading">
        <div>
          <h2 id="identity-heading" className="text-lg font-semibold">
            Sender identity
          </h2>
          <p className="text-muted-foreground text-sm">
            Who the mail is from. The system refuses to send until this is complete.
          </p>
        </div>
        {profiles.isPending && <Skeleton className="h-64 w-full" aria-label="Loading sender identity" />}
        {profiles.isError && (
          <p className="text-destructive text-sm" role="alert">
            Could not load the sender identity. Check that the backend is running, then refresh.
          </p>
        )}
        {profiles.data &&
          MODES.map(({ mode, title, blurb }) => {
            const slot = profiles.data.find((p) => p.mode === mode);
            return slot ? (
              <IdentityCard key={mode} slot={slot} title={title} blurb={blurb} client={client} />
            ) : null;
          })}
      </section>

      <section className="space-y-4" aria-labelledby="policy-heading">
        <div>
          <h2 id="policy-heading" className="text-lg font-semibold">
            Country rules
          </h2>
          <p className="text-muted-foreground text-sm">
            What may be sent to people in each country. The defaults are cautious (nothing goes to the EU
            until you decide otherwise) and are not legal advice. Review the countries you actually target.
          </p>
        </div>
        <PolicyTable client={client} />
      </section>
    </div>
  );
}
