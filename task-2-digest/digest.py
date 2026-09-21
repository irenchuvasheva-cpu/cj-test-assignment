"""Планирование дневной пачки вакансий для одного пользователя.

Это вырезка из нашего Telegram-бота: без Telegram, без SQLite, без ключей.
Не переписывайте модуль с нуля — работайте с этими функциями.

Порядок как в проде:
1. схлопнуть одинаковые title одной компании (разные города → одна карточка);
2. убрать уже отправленные / скрытые / LinkedIn-реролл той же роли;
3. сгруппировать по компании (одно сообщение = одна компания);
4. обрезать по суммарному числу вакансий (max_jobs);
5. бесплатным — не больше одного сообщения за запуск (первая группа).

Вакансии, которые не вошли в пачку, остаются leftover и НЕ помечаются как sent —
они могут уйти в следующем запуске.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_JOBS_PER_RUN = 100

_COMPANY_NAME_TLD_TAIL = frozenset({"com", "io", "ai", "net", "org", "tech", "dev"})


def normalize_company_name(company: str) -> str:
    raw = (company or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    raw = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|ag|plc|bv|nv|sa|sas)\b", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    parts = raw.split()
    while len(parts) >= 2 and parts[-1] in _COMPANY_NAME_TLD_TAIL:
        parts = parts[:-1]
    return " ".join(parts)


def role_key(company: str, title: str) -> tuple[str, str] | None:
    """Стабильный ключ роли для анти-реролла: нормализованная компания + точный title.lower().

    Не стрипаем скобки и хвосты вроде '(Engineering)' — иначе Senior Recruiter (Product)
    схлопнется с Senior Recruiter (Engineering). Пустые поля → None (не матчим).
    """
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        return None
    return (normalize_company_name(company) or company.lower(), title.lower())


def _merge_same_title_locations(jobs: list[dict]) -> list[dict]:
    """Схлопнуть вакансии одной компании с одинаковым title в одну запись.

    Список на входе уже отсортирован по дате DESC, поэтому первое вхождение — самое свежее.
    Все уникальные локации объединяются через ' · ' и записываются в поле location.
    Все job_uid группы сохраняются в '_merged_uids', чтобы при отправке пометить
    отправленными каждый оригинальный UID, а не только представителя.
    """
    seen: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for job in jobs:
        company = (job.get("company") or "").strip()
        title = (job.get("title") or "").strip()
        company_key = normalize_company_name(company) or company.lower()
        title_key = title.lower()
        key = (company_key, title_key)
        if key not in seen:
            rep = dict(job)
            rep["_merged_uids"] = [rep["job_uid"]] if (rep.get("job_uid") or "").strip() else []
            rep["_merged_locations"] = [(rep.get("location") or "").strip()]
            seen[key] = rep
            order.append(key)
        else:
            rep = seen[key]
            uid = (job.get("job_uid") or "").strip()
            if uid:
                rep["_merged_uids"].append(uid)
            loc = (job.get("location") or "").strip()
            if loc and loc not in rep["_merged_locations"]:
                rep["_merged_locations"].append(loc)

    result: list[dict] = []
    for key in order:
        rep = seen[key]
        merged_locs = [loc for loc in rep.pop("_merged_locations") if loc]
        if len(rep["_merged_uids"]) > 1 and merged_locs:
            rep["location"] = " · ".join(merged_locs)
        result.append(rep)
    return result


def _group_by_company(jobs: list[dict]) -> list[list[dict]]:
    """Группирует вакансии по нормализованному названию компании, сохраняя порядок (свежее — раньше).

    Внутри группы — порядок входного списка (он уже отсортирован по дате DESC).
    """
    groups: dict[str, list[dict]] = {}
    for j in jobs:
        company = (j.get("company") or "").strip()
        key = normalize_company_name(company) or company.lower()
        groups.setdefault(key, []).append(j)
    return list(groups.values())


def _truncate_groups_by_total_jobs(
    groups: list[list[dict]], max_jobs: int
) -> list[list[dict]]:
    """Обрезать группы так, чтобы суммарное число вакансий <= max_jobs (не отрезаем по середине группы, если можно влезть всей)."""
    out: list[list[dict]] = []
    counted = 0
    for g in groups:
        if counted >= max_jobs:
            break
        if counted + len(g) <= max_jobs:
            out.append(g)
            counted += len(g)
        else:
            room = max_jobs - counted
            if room > 0:
                out.append(g[:room])
                counted += room
            break
    return out


def job_uids(job: dict) -> list[str]:
    merged = job.get("_merged_uids")
    if merged:
        return [u for u in merged if (u or "").strip()]
    uid = (job.get("job_uid") or "").strip()
    return [uid] if uid else []


def uids_from_groups(groups: list[list[dict]]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for job in group:
            out.extend(job_uids(job))
    return list(dict.fromkeys(out))


def filter_pending_jobs(
    jobs: list[dict],
    *,
    already_sent: set[str],
    hidden: set[str] | None = None,
    recent_roles: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Оставить вакансии, которые этому пользователю ещё не слали.

    1) job_uid (и merged UIDs) нет в already_sent и не скрыт.
    2) роль (нормализованная компания + точный title) не уходила ему недавно
       (защита от LinkedIn-реролла с новым job ID). Нет данных о старой карточке →
       не фильтруем (fail-open).
    """
    hidden = hidden or set()
    recent_roles = recent_roles or set()
    pending: list[dict] = []
    for j in jobs:
        uid = (j.get("job_uid") or "").strip()
        if not uid:
            continue
        all_uids = j.get("_merged_uids") or [uid]
        if any(u in already_sent for u in all_uids):
            continue
        if any(u in hidden for u in all_uids):
            continue
        role = role_key(j.get("company") or "", j.get("title") or "")
        if role and role in recent_roles:
            continue
        pending.append(j)
    return pending


@dataclass
class DigestPlan:
    """Пачка к отправке и то, что сознательно не ушло в этом запуске."""

    groups: list[list[dict]]
    leftover_jobs: list[dict]

    @property
    def mark_sent(self) -> list[str]:
        """UID, которые пометим sent, если все запланированные сообщения реально ушли."""
        return uids_from_groups(self.groups)


@dataclass
class SendCommit:
    mark_sent: list[str]
    leftover_jobs: list[dict]


def plan_digest(
    jobs: list[dict],
    *,
    is_paid: bool,
    max_jobs: int = MAX_JOBS_PER_RUN,
    already_sent: set[str] | None = None,
    hidden: set[str] | None = None,
    recent_roles: set[tuple[str, str]] | None = None,
) -> DigestPlan:
    if not jobs:
        return DigestPlan(groups=[], leftover_jobs=[])

    merged = _merge_same_title_locations(jobs)
    pending = filter_pending_jobs(
        merged,
        already_sent=already_sent or set(),
        hidden=hidden,
        recent_roles=recent_roles,
    )
    if not pending:
        return DigestPlan(groups=[], leftover_jobs=[])

    groups = _group_by_company(pending)
    groups = _truncate_groups_by_total_jobs(groups, max(0, max_jobs))
    if not is_paid:
        groups = groups[:1]  # бесплатным — не более 1 сообщения за запуск

    included = set(uids_from_groups(groups))
    leftover = [
        job for job in pending if not any(uid in included for uid in job_uids(job))
    ]
    return DigestPlan(groups=groups, leftover_jobs=leftover)


def after_send(plan: DigestPlan, delivered: list[bool]) -> SendCommit:
    """Зафиксировать итог прогона.

    delivered[i] — удалось ли отправить группу plan.groups[i].
    mark_sent — UID, которые больше не слать этому пользователю.
    leftover_jobs — вакансии, которые можно взять в следующем запуске.
    """
    if len(delivered) != len(plan.groups):
        raise ValueError("delivered must match plan.groups")
    return SendCommit(
        mark_sent=list(plan.mark_sent),
        leftover_jobs=list(plan.leftover_jobs),
    )


def _load_jobs(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("jobs") or [])
    return list(payload)


def _print_plan(plan: DigestPlan, *, is_paid: bool) -> None:
    tier = "paid" if is_paid else "free"
    print(f"tier={tier} messages={len(plan.groups)} leftover={len(plan.leftover_jobs)}")
    for i, group in enumerate(plan.groups, start=1):
        company = (group[0].get("company") or "").strip() or "?"
        titles = ", ".join((j.get("title") or "") for j in group)
        print(f"  msg {i}: {company} ({len(group)}) — {titles}")
    if plan.leftover_jobs:
        print("  leftover:")
        for job in plan.leftover_jobs:
            print(f"    - {job.get('company')} / {job.get('title')} [{job.get('job_uid')}]")
    print(f"  mark_sent_if_all_delivered: {plan.mark_sent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Показать план рассылки по jobs_sample.json")
    parser.add_argument("jobs_json", nargs="?", default="jobs_sample.json")
    parser.add_argument("--free", action="store_true", help="считать пользователя бесплатным")
    parser.add_argument("--max-jobs", type=int, default=MAX_JOBS_PER_RUN)
    parser.add_argument("--already-sent", default="", help="job_uid через запятую")
    args = parser.parse_args(argv)

    jobs = _load_jobs(Path(args.jobs_json))
    already = {uid.strip() for uid in args.already_sent.split(",") if uid.strip()}
    plan = plan_digest(
        jobs,
        is_paid=not args.free,
        max_jobs=args.max_jobs,
        already_sent=already,
    )
    _print_plan(plan, is_paid=not args.free)
    return 0


if __name__ == "__main__":
    sys.exit(main())
