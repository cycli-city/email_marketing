# Sending rules and compliance

What the system will and will not send, why, and what you have to do before the first campaign.
Decisions: ADR-010 (country policies as data), ADR-043 (the gate and the unsubscribe flow).

> **This is not legal advice.** The defaults below are deliberately cautious and exist so that nothing goes out before
> somebody has thought about it. The law differs by country, changes, and depends on facts about you and the recipient that
> software cannot know. You are responsible for the rules that apply to you. If a campaign matters, ask a lawyer.

## 1. Rules that cannot be switched off

Every message passes one gate immediately before it is sent. It is checked **again at that moment**, so something that
changed after the message was queued (an unsubscribe, a bounce, a reply) is honoured. The gate lists *every* reason a message
is blocked, not just the first, and shows them to you.

A message is never sent when:

| Rule | Why |
|---|---|
| The address was **guessed** from a name pattern | Guessed addresses reach the wrong people and hurt your sender reputation. The system never creates one. |
| The address is **invalid**, or verification called it **risky** (catch-all domain, disposable mailbox) | A bounce is a signal providers use to throttle or block you. |
| The address, its Gmail look-alikes (`j.doe+x@` = `jdoe@`), or its **domain** is on the do-not-contact list | An opt-out is final. |
| The **lead is closed** (unsubscribed, bounced, suppressed, duplicate, disqualified) | Nothing automatic is sent to a finished lead. A follow-up also stops once the person has replied, the sequence is exhausted, or a human has to look. |
| The **sender is not named** | Every message says who it is from. |
| The address is not a valid e-mail address | — |

## 2. What every sales message carries

* the **sender's name** and **postal address** (from *Settings → Sending*),
* a signed **unsubscribe link** that works forever, plus the one-click header that mail clients use (RFC 8058).

Opening the unsubscribe link only shows a confirmation page — mail scanners open links, and they must not be able to
unsubscribe people. Pressing the button (or a mail client's one-click button) does it: the address joins the do-not-contact
list **permanently**, everything still waiting for it is cancelled, and every lead it belongs to stops. Doing it twice
changes nothing.

Job applications (Job Hunter) are not marketing: they need your name but no postal address or footer, and the cold-marketing
country rules do not apply. Suppression, address quality and closed leads still do.

## 3. Rules you can tune (Settings → Sending)

* **Per country**: whether cold e-mail to *companies* and to *individuals* is *allowed* (with an opt-out), *needs recorded
  consent*, or is *never* allowed; and whether a postal address and an unsubscribe link are required.
  *Never* is absolute: consent on record does not lift it.
* **Making sending easier** (relaxing a rule, dropping a requirement) asks for an explicit confirmation and is written to the
  audit log with the before and after. Tightening needs no confirmation.
* **Per campaign**: whether unverified addresses may be used (they then still need a recorded source page), whether shared
  mailboxes such as `info@` may be written to, and the cooldown.
* **Cooldown**: an address mailed in *any* campaign is not mailed again as a first message for 180 days (follow-ups are exempt).

An address whose subscriber type is unknown is judged by the **stricter** of the two rules (company / individual).
Discovery classifies free-mail and disposable addresses as *individuals* and addresses at a business's own domain as
*companies*. That is the usual B2B reading but it is **not always the legal one**: in the UK and EU a sole trader or an
ordinary partnership at their own domain counts as an individual. If you target those, review the country row.

## 4. The defaults

<!-- The lists below are checked against app/domain/policy_defaults.py by tests/unit/test_compliance_doc.py. -->

| Country | Cold e-mail to companies | Cold e-mail to individuals |
|---|---|---|
| US | allowed, with an opt-out | allowed, with an opt-out |
| CA | allowed (the address must have been published; the gate requires a recorded source page) | needs recorded consent |
| GB | allowed, with an opt-out | needs recorded consent |
| Other opt-out countries: AU, NZ, IN, SG, AE, IL, ZA, BR, MX, JP, KR, HK, MY, PH, ID, TH, VN | allowed, with an opt-out | needs recorded consent |
| EU / EEA: AT, BE, BG, HR, CY, CZ, DK, EE, FI, FR, DE, GR, HU, IS, IE, IT, LV, LI, LT, LU, MT, NL, NO, PL, PT, RO, SK, SI, ES, SE | **needs recorded consent** | **needs recorded consent** |
| Everywhere else (`ZZ`) | allowed, with an opt-out | needs recorded consent |

So by default **nothing is cold-mailed to the EU/EEA** until you review the country and decide. Every default row starts
as "not reviewed"; the Settings page counts them.

## 5. Before the first real campaign

1. **Settings → Sending → NEXORA Sales**: enter your name and the **postal address** that will appear in every email.
   The system refuses to send without it.
2. Review the **countries you actually target** and mark them reviewed.
3. Connect the mailbox (Settings → Integrations) and keep the volume low at first — the sending limits ramp up
   gradually and pause by themselves if bounces rise.
4. Remember the reply-to-opt-out: a person who answers "please remove me" must be suppressed. That is built in the reply
   handling (Phase 5); until it ships, such replies need your attention in the inbox.

## 6. What is recorded

* Every address's **source**: where it was found, by which provider or page, and when.
* Every verification attempt and its result.
* Every suppression (reason, source) and every policy change (who, before, after) in the audit log.
* The lawful basis the gate relied on for each message that was allowed.
* Activity-feed entries name *messages*, never e-mail addresses.

## 7. Known limits

* Reading an unsubscribe **reply** ("stop", "remove me") and a `mailto:` opt-out arrives with reply classification.
* Consent evidence is **recorded** (a source and a note) but this system cannot judge whether it is sufficient.
* The classification of sole traders (above) is a heuristic.
* The country rules are a starting point, not a legal opinion, and the list of countries is not exhaustive.
