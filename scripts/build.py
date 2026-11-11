#!/usr/bin/env python3
"""Rebuild every published file from one Tiny Tiny RSS OPML export.

    python3 scripts/build.py path/to/tt-rss-export.opml

Writes, relative to the repository root:

    tt-rss/feeds-ttrss.opml   feeds + labels + filters, for Tiny Tiny RSS
    feeds.opml                plain OPML 2.0, for any other reader
    opml/<n>-<tier>.opml      one plain OPML per tier
    feeds.csv                 flat catalogue
    FEEDS.md                  browsable catalogue
    docs/TRIAGE.md            labels and filters, with their regexes
    README.md                 only the block between the "stats" markers

What is removed from the export on the way:
  * the "tt-rss-prefs" section, so importing never changes anyone's settings;
  * the quarantine category (hijacked or spam feeds) and every filter that
    points at a feed or category which is not shipped. A filter whose target
    is missing would otherwise import as a global rule;
  * per-instance leftovers in filters: "last triggered" timestamps and
    numeric feed ids in filter titles;
  * site links that are placeholders or local addresses.
"""
import csv
import json
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUARANTINE = "Quarantine"

# One line per category: what belongs there.
PURPOSE = {
    "Maldev & AV/EDR Evasion": "Implant and loader development, AV/EDR evasion, injection, shellcode, BOFs.",
    "Offensive Tool Dev & C2": "C2 frameworks, implants and offensive tooling releases.",
    "Red Team Ops & Adversary Emulation": "Operations tradecraft, infrastructure, planning and adversary simulation.",
    "AD & Identity Attacks": "Active Directory, ADCS, Kerberos and NTLM relay, Entra attack paths.",
    "Cloud · Offensive Research": "AWS, Azure and GCP attack techniques.",
    "Pentest Tradecraft": "Consultancy blogs and generalist offensive write-ups.",
    "Research Labs & Mixed VR": "Multi-platform research labs publishing across targets.",
    "VR · Windows Internals & Kernel": "Windows internals, kernel and driver bugs, local privilege escalation.",
    "VR · Linux Kernel & Binary Exploitation": "Linux kernel, heap and pwn, embedded binary exploitation.",
    "VR · Apple (macOS/iOS)": "macOS and iOS internals, TCC, sandbox, kernel.",
    "Web & API Vulnerability Research": "Web and API vulnerability classes and original research.",
    "Bug Bounty Write-ups": "Bounty reports and hunter write-ups.",
    "CTF · Write-ups": "CTF, HTB and wargame write-ups.",
    "CTF · Training, Labs & Certs": "Certification reviews, learning paths, practice labs.",
    "VR · Product Advisories & Disclosures": "Researcher-side advisories and multi-CVE product disclosures.",
    "Advisories · Gov & CERT": "National CERTs and CSIRTs, ISACs, government alerts.",
    "Advisories · Vendor PSIRT & Patches": "Vendor security bulletins and distribution advisories.",
    "Vulns · Exploitation & Rapid Response": "In-the-wild exploitation, n-day analysis, patch-prioritisation input.",
    "Vulns · CVE & Exploit Feeds": "Machine CVE and exploit streams. Meant to be filtered, not read raw.",
    "Threat Intel · Actors & Campaigns": "Vendor CTI teams, nation-state and campaign reporting.",
    "Malware Analysis & RE": "Sample deep-dives, unpacking, malware reverse engineering.",
    "Threat Intel · Cybercrime, Fraud & Phishing": "Underground economy, phishing kits, fraud, crypto-crime.",
    "Breach, IOC & Attack Tracking": "Breach notifications, IOC platforms, attack timelines.",
    "News · Cybersecurity": "Security journalism.",
    "Detection Engineering & Threat Hunting": "Detection rules, SIEM queries, hunting, SOC engineering.",
    "DFIR & Forensics": "Forensic artefacts, incident-response case studies, DFIR tooling.",
    "Purple Team & General Defense": "Purple teaming, defensive programmes, mixed blue-team content.",
    "Cloud · Identity (Entra, IAM, OAuth)": "Identity platforms, Entra ID, OAuth and OIDC, zero trust.",
    "Cloud · Platform & Posture": "Cloud provider and CNAPP blogs, Kubernetes, posture management.",
    "Hardware, IoT & RF": "Hardware hacking, glitching, IoT devices, SDR and RF.",
    "OT / ICS": "Industrial control systems and critical infrastructure.",
    "AI & LLM Security": "Prompt injection, agent security, AI red teaming.",
    "AppSec, Supply Chain & DevSecOps": "Package ecosystems, CI/CD, secure development.",
    "Cryptography & PKI": "Applied cryptography, WebPKI, post-quantum.",
    "Mobile Security (Android, iOS, Baseband)": "Android and iOS internals, app reversing, baseband and firmware bugs.",
    "Web3, DeFi & Blockchain Security": "Smart-contract exploits, DeFi post-mortems, chain security.",
    "OSINT & Recon": "OSINT, investigations, reconnaissance.",
    "GRC, Policy & Privacy": "Regulation, compliance, privacy, digital rights.",
    "Industry Voices & Leadership": "CISO and practitioner commentary, essays.",
    "Newsletters & Digests": "Curated weekly digests.",
    "Talks, Podcasts & Conferences": "Conference talks, podcasts, video channels.",
    "Academic & Research Papers": "arXiv, university labs, peer-reviewed venues.",
    "Tools & Releases": "Security tool release trackers and changelogs.",
    "Vendor · Product & Corporate": "Vendor product and corporate blogs.",
    "Community · Reddit & Aggregators": "Subreddits and link aggregators. Noisy; skim only.",
    "News · Tech & Science": "General technology, science and business news.",
    "Engineering · Dev, Sysadmin & Infra": "Programming, Linux, networking, developer communities.",
    "Personal & Off-topic": "Personal blogs that drifted off security; kept for reference.",
    "Archive · Dormant (>12 mo)": "No post in more than twelve months. Kept, not deleted.",
    "Archive · Broken (repair queue)": "Fetch errors and no recent content. Needs a URL fix.",
}


# One line per tier: what to read it for (shown in the README table).
TIER_PURPOSE = {
    "Offensive Research & Red Team": "Maldev and EDR evasion, C2 and tooling, AD and identity attacks, vulnerability research by platform, web and API research, bug bounty and CTF write-ups.",
    "Advisories & Exploitation": "What needs action: CERT alerts, vendor bulletins, in-the-wild exploitation, raw CVE and exploit streams.",
    "Threat Intelligence": "Actors and campaigns, malware analysis, cybercrime and fraud, breach tracking, security news.",
    "Defense & Domains": "Detection engineering, DFIR, purple teaming, plus cloud, hardware and RF, OT/ICS, AI security, supply chain, cryptography, mobile, Web3, OSINT.",
    "Strategy & Learning": "Policy and privacy, practitioner commentary, newsletters, talks and podcasts, papers, tool releases, vendor blogs, aggregators.",
    "Background (non-security)": "General tech news, engineering blogs, off-topic personal blogs.",
    "Archive": "No post in over a year, or currently failing. Kept because old research stays useful and blogs come back.",
}


def bare(title):
    """Category title without its leading emoji or tier number."""
    return re.sub(r"^[^A-Za-z]+", "", title).strip()


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", bare(title).lower()).strip("-")


def anchor(heading):
    """GitHub's heading anchor: keep word characters, marks, joiners, hyphens and spaces."""
    keep = []
    for ch in heading.strip().lower():
        cat = unicodedata.category(ch)
        if ch in "- " or cat[0] in "LM" or cat in ("Nd", "Pc") or ch in "\u200c\u200d":
            keep.append(ch)
    return "".join(keep).replace(" ", "-")


def md(text):
    return (text or "").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").strip()


def usable_site(url):
    """False for a missing site URL, a literal "None", or a local address."""
    url = (url or "").strip()
    if url.lower() in ("", "none", "null"):
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if "." not in host or host == "0.0.0.0" or host.startswith(("127.", "10.", "192.168.")):
        return False
    return True


# Filter titles as published, where they differ from the working instance.
FILTER_TITLES = {
    "Noise — CrowdStrike marketing": "Focus — CrowdStrike research posts only",
}


def load(export):
    body = ET.parse(export).getroot().find("body")
    tiers, labels, filters = [], [], []
    for node in body:
        name = node.get("text") or ""
        if name == "tt-rss-prefs":
            continue
        if name == "tt-rss-labels":
            labels = list(node)
        elif name == "tt-rss-filters":
            filters = list(node)
        elif len(node) and not node.get("xmlUrl"):
            cats = []
            for cat in node:
                if cat.get("xmlUrl") or QUARANTINE in (cat.get("text") or ""):
                    continue
                feeds = [f for f in cat if f.get("xmlUrl")]
                for f in feeds:  # drop site links that are placeholders or not routable
                    if not usable_site(f.get("htmlUrl")):
                        f.attrib.pop("htmlUrl", None)
                if feeds:
                    cats.append((cat, feeds))
            if cats:
                tiers.append((node, cats))
    return tiers, labels, filters


def keep_filters(filters, tiers):
    """Drop filters whose rules point at something that is not shipped."""
    known = {t.get("text") for t, _ in tiers}
    for _, cats in tiers:
        for cat, feeds in cats:
            known.add(cat.get("text"))
            known.update(f.get("text") for f in feeds)
    kept, dropped = [], []
    for f in filters:
        spec = json.loads(f.text)
        targets = [r.get("feed") for r in spec["rules"] if r.get("feed")]
        if not all(t in known for t in targets):
            dropped.append(spec["title"])
            continue
        spec["last_triggered"] = None
        spec["title"] = re.sub(r"\s*\(\d+\)$", "", spec["title"])
        for old, new in FILTER_TITLES.items():
            spec["title"] = spec["title"].replace(old, new)
        kept.append(spec)
    seen = {}
    for spec in kept:
        seen[spec["title"]] = seen.get(spec["title"], 0) + 1
    for spec in kept:
        feeds = [r["feed"] for r in spec["rules"] if r.get("feed") and not r.get("cat_filter")]
        if seen[spec["title"]] > 1 and feeds:
            spec["title"] += " · " + feeds[0]
    return kept, dropped


def indent(tree):
    ET.indent(tree, space="  ")


def write_ttrss(path, tiers, labels, filters):
    opml = ET.Element("opml", version="1.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "CybeRSS (Tiny Tiny RSS: feeds, labels, filters)"
    body = ET.SubElement(opml, "body")
    for tier, cats in tiers:
        t = ET.SubElement(body, "outline", dict(tier.attrib))
        for cat, feeds in cats:
            c = ET.SubElement(t, "outline", dict(cat.attrib))
            for f in feeds:
                ET.SubElement(c, "outline", dict(f.attrib))
    lab = ET.SubElement(body, "outline", text="tt-rss-labels", schemaVersion="1")
    for node in labels:
        ET.SubElement(lab, "outline", dict(node.attrib))
    fil = ET.SubElement(body, "outline", text="tt-rss-filters", schemaVersion="1")
    for spec in filters:
        ET.SubElement(fil, "outline", {"filter-type": "2"}).text = json.dumps(spec)
    tree = ET.ElementTree(opml)
    indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_plain(path, title, tiers):
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(opml, "body")
    for tier, cats in tiers:
        t = ET.SubElement(body, "outline", text=tier.get("text"), title=tier.get("text"))
        for cat, feeds in cats:
            c = ET.SubElement(t, "outline", text=cat.get("text"), title=cat.get("text"))
            for f in feeds:
                attrs = {"type": "rss", "text": f.get("text"), "title": f.get("text"), "xmlUrl": f.get("xmlUrl")}
                if f.get("htmlUrl"):
                    attrs["htmlUrl"] = f.get("htmlUrl")
                ET.SubElement(c, "outline", attrs)
    tree = ET.ElementTree(opml)
    indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def paused(feed):
    return feed.get("ttrssUpdateInterval") == "-1"


def write_csv(path, tiers):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["tier", "category", "title", "feed_url", "site_url", "polling"])
        for tier, cats in tiers:
            for cat, feeds in cats:
                for f in feeds:
                    w.writerow([tier.get("text"), cat.get("text"), f.get("text"), f.get("xmlUrl"),
                                f.get("htmlUrl") or "", "paused" if paused(f) else "on"])


def write_catalogue(path, tiers):
    total = sum(len(feeds) for _, cats in tiers for _, feeds in cats)
    out = ["# Feed catalogue", "",
           f"{total:,} feeds in {sum(len(c) for _, c in tiers)} categories under {len(tiers)} tiers. "
           "Generated by `scripts/build.py`; do not edit by hand.", "",
           "⏸ marks a feed whose polling is paused, usually because the site answers automated "
           "clients with a bot challenge or no longer serves a feed.", ""]
    for tier, cats in tiers:
        n = sum(len(feeds) for _, feeds in cats)
        out.append(f"- [{tier.get('text')}](#{anchor(tier.get('text'))}) — {n}")
        for cat, feeds in cats:
            out.append(f"  - [{cat.get('text')}](#{anchor(cat.get('text'))}) — {len(feeds)}")
    out.append("")
    for tier, cats in tiers:
        out += [f"## {tier.get('text')}", ""]
        for cat, feeds in cats:
            out += [f"### {cat.get('text')}", ""]
            purpose = PURPOSE.get(bare(cat.get("text")))
            if purpose:
                out += [purpose, ""]
            for f in sorted(feeds, key=lambda x: (x.get("text") or "").lower()):
                site = f.get("htmlUrl") or f.get("xmlUrl")
                flag = " ⏸" if paused(f) else ""
                out.append(f"- [{md(f.get('text'))}]({site}) · [feed]({f.get('xmlUrl')}){flag}")
            out.append("")
    Path(path).write_text("\n".join(out), encoding="utf-8")


ACTIONS = {1: "delete", 2: "mark as read", 3: "star", 4: "tag", 5: "publish", 6: "score",
           7: "label", 8: "stop", 9: "plugin", 10: "ignore tag"}
FIELDS = {1: "title", 2: "content", 3: "title or content", 4: "link", 5: "date", 6: "author", 7: "tag"}


def code(text):
    """Inline code that survives a Markdown table cell."""
    return "<code>" + (text.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")) + "</code>"


def write_triage(path, labels, filters):
    out = ["# Triage: labels and filters", "",
           "Generated by `scripts/build.py` from `tt-rss/feeds-ttrss.opml`; do not edit by hand.", "",
           "Tiny Tiny RSS applies filters when an article is fetched. Every rule here is a "
           "case-insensitive regular expression. A rule marked *inverse* fires when the "
           "expression does **not** match.", "",
           "## Labels", "", "| label | colour |", "|---|---|"]
    for node in labels:
        out.append(f"| {md(node.get('label-name'))} | `{node.get('label-bg-color')}` |")
    out += ["", "## Filters", ""]
    for spec in filters:
        acts = []
        for a in spec["actions"]:
            name = ACTIONS.get(int(a["action_id"]), f"action {a['action_id']}")
            acts.append(f"{name} → {a['action_param']}" if a.get("action_param") else name)
        out += [f"### {spec['title']}", "", f"Action: **{'; '.join(acts)}**", "",
                "| applies to | field | inverse | expression |", "|---|---|---|---|"]
        rules = {}
        for r in spec["rules"]:
            key = (FIELDS.get(int(r["filter_type"]), "?"), bool(r.get("inverse")), r["reg_exp"])
            scope = ("category " if r.get("cat_filter") else "feed ") + f"“{r['feed']}”" if r.get("feed") else "every feed"
            rules.setdefault(key, []).append(scope)
        for (field, inverse, regex), scopes in rules.items():
            out.append(f"| {md('; '.join(scopes))} | {field} | {'yes' if inverse else 'no'} | {code(regex)} |")
        out.append("")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def write_readme_stats(path, tiers, labels, filters):
    """Replace the block between the stats markers in README.md."""
    path = Path(path)
    if not path.exists():
        return
    feeds = [f for _, cats in tiers for _, fs in cats for f in fs]
    rows = ["| tier | categories | feeds | read it for |", "|---|---:|---:|---|"]
    for i, (tier, cats) in enumerate(tiers, 1):
        link = f"[{tier.get('text')}](opml/{i}-{slug(tier.get('text'))}.opml)"
        purpose = TIER_PURPOSE.get(bare(tier.get("text")), "")
        rows.append(f"| {link} | {len(cats)} | {sum(len(fs) for _, fs in cats)} | {purpose} |")
    rows.append(f"| **total** | **{sum(len(c) for _, c in tiers)}** | **{len(feeds):,}** | |")
    block = "\n".join(["<!-- stats:start -->", *rows, "<!-- stats:end -->"])
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"<!-- stats:start -->.*?<!-- stats:end -->", lambda _: block, text, flags=re.S)
    if n:
        path.write_text(new, encoding="utf-8")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    tiers, labels, filters = load(sys.argv[1])
    filters, dropped = keep_filters(filters, tiers)

    missing = sorted({bare(c.get("text")) for _, cats in tiers for c, _ in cats} - set(PURPOSE))
    if missing:
        sys.exit(f"no PURPOSE entry for: {missing}")

    (ROOT / "tt-rss").mkdir(exist_ok=True)
    (ROOT / "opml").mkdir(exist_ok=True)
    write_ttrss(ROOT / "tt-rss" / "feeds-ttrss.opml", tiers, labels, filters)
    write_plain(ROOT / "feeds.opml", "CybeRSS", tiers)
    for old in (ROOT / "opml").glob("*.opml"):
        old.unlink()
    for i, (tier, cats) in enumerate(tiers, 1):
        write_plain(ROOT / "opml" / f"{i}-{slug(tier.get('text'))}.opml",
                    f"CybeRSS: {bare(tier.get('text'))}", [(tier, cats)])
    write_csv(ROOT / "feeds.csv", tiers)
    write_catalogue(ROOT / "FEEDS.md", tiers)
    (ROOT / "docs").mkdir(exist_ok=True)
    write_triage(ROOT / "docs" / "TRIAGE.md", labels, filters)
    write_readme_stats(ROOT / "README.md", tiers, labels, filters)

    feeds = [f for _, cats in tiers for _, fs in cats for f in fs]
    print(f"tiers {len(tiers)}  categories {sum(len(c) for _, c in tiers)}  feeds {len(feeds)}  "
          f"paused {sum(paused(f) for f in feeds)}  labels {len(labels)}  filters {len(filters)}")
    for title in dropped:
        print("dropped filter:", title)
    for tier, cats in tiers:
        print(f"  {tier.get('text')}: {len(cats)} categories, {sum(len(fs) for _, fs in cats)} feeds")


if __name__ == "__main__":
    main()
