import re
from difflib import SequenceMatcher

from django.db.models import Case, IntegerField, When


def normalize_search(value):
    value = (value or '').casefold().replace('ё', 'е')
    return re.sub(r'[^a-zа-я0-9@]+', ' ', value).strip()


def fuzzy_score(query, *values):
    query = normalize_search(query)
    if not query:
        return 0.0
    compact_query = query.replace(' ', '')
    best = 0.0
    for value in values:
        text = normalize_search(value)
        if not text:
            continue
        compact_text = text.replace(' ', '')
        if query in text or compact_query in compact_text:
            best = max(best, 1.0)
            continue
        candidates = [text, compact_text]
        candidates.extend(token for token in re.split(r'[\s@._-]+', (value or '').casefold()) if token)
        for candidate in candidates:
            best = max(best, SequenceMatcher(None, compact_query, candidate.replace(' ', '')).ratio())
    return best


def fuzzy_rank_mailboxes(queryset, query, limit=None):
    query = (query or '').strip()
    if not query:
        return queryset.none()
    scored = []
    for mailbox in queryset.select_related('client', 'region', 'manager__manager_profile'):
        owner = getattr(getattr(mailbox.manager, 'manager_profile', None), 'phone', '')
        score = fuzzy_score(
            query,
            mailbox.email,
            mailbox.display_name,
            mailbox.owner_phone,
            owner,
            getattr(mailbox.client, 'full_name', ''),
            getattr(mailbox.region, 'name', ''),
            getattr(mailbox.region, 'city', ''),
        )
        threshold = 0.72 if len(normalize_search(query)) <= 3 else 0.54
        if score >= threshold:
            scored.append((score, mailbox.pk))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ids = [pk for _, pk in scored[:limit]]
    if not ids:
        return queryset.none()
    ordering = Case(
        *[When(pk=pk, then=position) for position, pk in enumerate(ids)],
        output_field=IntegerField(),
    )
    return queryset.filter(pk__in=ids).order_by(ordering)
