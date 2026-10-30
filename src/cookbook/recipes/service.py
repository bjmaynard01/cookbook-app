"""Caller-transaction-managed recipe persistence services.

Create and update persist a complete recipe aggregate while leaving
transaction control with the caller. Delete protects aliased recipes, and
the read service returns an ordered aggregate record. Write services flush
but never commit or roll back the caller's transaction.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ..models import (
    Category,
    Recipe,
    RecipeItem,
    RecipeSection,
    RecipeUrlAlias,
    Tag,
    recipe_categories,
    recipe_tags,
)
from .drafts import RecipeDraft
from .errors import (
    InvalidRecipeInput,
    RecipeAliasConflict,
    RecipeHasAliases,
    RecipeNotFound,
    RecipeServiceError,
    UnknownCategories,
)
from .records import RecipeDetails, RecipeSectionDetails
from .slugs import slugify
from .validation import (
    RecipeSaveData,
    RecipeValidationError,
    validate_for_save,
)


# --- model width limits (single source of truth for the service) ---------
TITLE_MAX = 300
SERVINGS_TEXT_MAX = 100
SECTION_HEADING_MAX = 200
INGREDIENT_QUANTITY_MAX = 50
INGREDIENT_UNIT_MAX = 50
INGREDIENT_ITEM_MAX = 300
INGREDIENT_PREPARATION_MAX = 300
TAG_NAME_MAX = 100
SOURCE_SYSTEM_MAX = 20
SOURCE_ID_MAX = 64
SOURCE_NAME_MAX = 300
SOURCE_URL_MAX = 2000
EXTRACTION_METHOD_MAX = 20
ACTOR_ID_MAX = 36
SLUG_MAX = 200  # Recipe.slug and Tag.slug

# Highest numeric suffix tried for collision resolution. The suffix text is
# ``-<n>`` (up to 6 chars), which fits comfortably in SLUG_MAX at n <= 99999.
_SLUG_SUFFIX_START = 2
_SLUG_SUFFIX_MAX = 99999


def _fit_suffix(base: str, suffix: int) -> str | None:
    """Shorten ``base`` so ``<base>-<suffix>`` fits SLUG_MAX chars, or None."""
    suffix_text = f"-{suffix}"
    room = SLUG_MAX - len(suffix_text)
    if len(base) > room:
        base = base[:room].rstrip("-")
    if not base:
        return None
    return base + suffix_text


def _over_too_long(field: str, value: str, limit: int) -> str:
    return (
        f"{field} exceeds the allowed width of {limit} characters "
        f"(got {len(value)})."
    )


def _dedupe_tags_case_insensitive(tags: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for raw in tags:
        trimmed = raw.strip()
        if not trimmed:
            continue
        key = trimmed.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(trimmed)
    return unique


def _line_text(line: object) -> str:
    # RecipeIngredientDraft.raw_text vs RecipeStepDraft.body (DL-3).
    if hasattr(line, "raw_text"):
        return line.raw_text  # type: ignore[no-any-return]
    return line.body  # type: ignore[union-attr]


def _check_widths(save_data: RecipeSaveData) -> None:
    """Width-check every VARCHAR field the service would write.

    Reports every offending field in one :class:`InvalidRecipeInput` (no
    truncation — loud failure over silent data loss).
    """
    errors: dict[str, str] = {}

    def _check(field: str, value: str | None, limit: int, key: str) -> None:
        if value is not None and len(value) > limit:
            errors[key] = _over_too_long(field, value, limit)

    _check("title", save_data.title, TITLE_MAX, "title")
    _check(
        "servings_text", save_data.servings_text, SERVINGS_TEXT_MAX,
        "servings_text",
    )

    for i, section in enumerate(save_data.sections):
        _check(
            "section.heading", section.heading, SECTION_HEADING_MAX,
            f"sections[{i}].heading",
        )
        if section.kind != "ingredients":
            continue
        for j, line in enumerate(section.lines):
            prefix = f"sections[{i}].lines[{j}]"
            _check(
                "quantity_text", line.quantity_text, INGREDIENT_QUANTITY_MAX,
                f"{prefix}.quantity_text",
            )
            _check("unit", line.unit, INGREDIENT_UNIT_MAX, f"{prefix}.unit")
            _check("item", line.item, INGREDIENT_ITEM_MAX, f"{prefix}.item")
            _check(
                "preparation", line.preparation, INGREDIENT_PREPARATION_MAX,
                f"{prefix}.preparation",
            )

    for t, tag in enumerate(save_data.tags):
        _check("tag.name", tag, TAG_NAME_MAX, f"tags[{t}]")

    if save_data.provenance is not None:
        p = save_data.provenance
        _check("source_system", p.source_system, SOURCE_SYSTEM_MAX, "provenance.source_system")
        _check("source_id", p.source_id, SOURCE_ID_MAX, "provenance.source_id")
        _check("source_name", p.source_name, SOURCE_NAME_MAX, "provenance.source_name")
        _check("source_url", p.source_url, SOURCE_URL_MAX, "provenance.source_url")
        _check(
            "extraction_method", p.extraction_method, EXTRACTION_METHOD_MAX,
            "provenance.extraction_method",
        )

    if errors:
        raise InvalidRecipeInput(errors)


async def _slug_in_use(
    session: AsyncSession, slug: str, *, current_read: bool = False
) -> bool:
    """True if a recipe slug or alias path ``/r/<slug>`` is reserved.

    Collision retries use a locking/current read so they see a concurrent
    winner even under MariaDB's default REPEATABLE READ isolation. The initial
    availability scan stays non-locking to avoid serializing ordinary creates.
    """
    recipe_query = select(Recipe.id).where(Recipe.slug == slug).limit(1)
    alias_query = (
        select(RecipeUrlAlias.id)
        .where(RecipeUrlAlias.path == f"/r/{slug}")
        .limit(1)
    )
    if current_read:
        recipe_query = recipe_query.with_for_update()
        alias_query = alias_query.with_for_update()

    slug_row = (await session.execute(recipe_query)).first()
    if slug_row is not None:
        return True
    alias_row = (await session.execute(alias_query)).first()
    return alias_row is not None


async def _pick_free_slug(
    session: AsyncSession,
    base: str,
    *,
    current_read: bool = False,
    start_suffix: int = _SLUG_SUFFIX_START,
    include_base: bool = True,
) -> str:
    """First unused slug from ``base``: plain, then ``base-2``, ``base-3``,
    ... shortening the base as needed to stay inside SLUG_MAX.

    ``current_read=True`` is reserved for post-conflict retries in a caller's
    REPEATABLE READ transaction, where an ordinary SELECT would reuse the old
    snapshot and miss a concurrently committed winner.
    """
    if include_base and base and not await _slug_in_use(
        session, base, current_read=current_read
    ):
        return base
    for suffix in range(max(start_suffix, _SLUG_SUFFIX_START), _SLUG_SUFFIX_MAX + 1):
        candidate = _fit_suffix(base, suffix)
        if candidate is None or await _slug_in_use(
            session, candidate, current_read=current_read
        ):
            continue
        return candidate
    raise RecipeServiceError("Could not allocate a free recipe slug.")


def _next_suffix_after_collision(base: str, candidate: str) -> int:
    """Return the first numeric suffix after a candidate that raced on insert."""
    if candidate == base:
        return _SLUG_SUFFIX_START
    suffix = candidate.rpartition("-")[2]
    if suffix.isdecimal():
        return max(_SLUG_SUFFIX_START, int(suffix) + 1)
    return _SLUG_SUFFIX_START


def _is_slug_duplicate(exc: BaseException) -> bool:
    """True iff ``exc`` is a duplicate-entry integrity error on a slug column.

    MariaDB error 1062 carries the offending key in its text; the key on
    ``recipes.slug`` is named after the column (``recipes.slug`` / ``slug``).
    We require both the 1062 code and a slug mention so unrelated integrity
    failures (FK violations, checks) are never swallowed.
    """
    if not isinstance(exc, IntegrityError):
        return False
    detail = str(exc.orig if getattr(exc, "orig", None) is not None else exc).lower()
    return "1062" in detail and "slug" in detail


async def _current_tag_rows(
    session: AsyncSession,
) -> list[tuple[str, str, str]]:
    """Read committed tags outside the caller's repeatable-read snapshot.

    A unique-key retry can discover a tag committed after the caller's first
    consistent read. On the same MariaDB transaction, a later locking read of
    that changed table can fail with error 1020. A short-lived read-only
    connection gives the resolver a current committed view without changing
    the caller's transaction. Tags already written by this caller are merged
    from its identity map so repeated saves in one transaction reuse them.
    """
    bind = session.bind
    engine = bind if isinstance(bind, AsyncEngine) else bind.engine
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(Tag.id, Tag.name, Tag.slug))
        ).all()

    by_id = {row.id: (row.id, row.name, row.slug) for row in rows}
    local_tags = [*session.identity_map.values(), *session.new]
    for tag in local_tags:
        if isinstance(tag, Tag) and tag.id is not None and tag.name is not None:
            by_id[tag.id] = (tag.id, tag.name, tag.slug)
    return list(by_id.values())


async def _ensure_tag(session: AsyncSession, name: str) -> str:
    """Return the id of a trim/casefold-matched tag, creating it if absent.

    Name comparison runs in Python so leading/trailing whitespace and Unicode
    casefolding are handled consistently. A unique slug race is retried in a
    savepoint; a second short-lived connection observes a committed same-name
    winner even when the caller's transaction uses REPEATABLE READ.
    """
    target_key = name.strip().casefold()
    base = slugify(name, fallback="tag", max_length=SLUG_MAX)
    rows = (
        await session.execute(select(Tag.id, Tag.name, Tag.slug))
    ).all()

    def _matching_id(tag_rows: Sequence[tuple[str, str, str]]) -> str | None:
        matches = [row for row in tag_rows if row[1].strip().casefold() == target_key]
        return min(matches, key=lambda row: row[0])[0] if matches else None

    existing_id = _matching_id(rows)
    if existing_id is not None:
        return existing_id

    taken = {row[2] for row in rows}

    def _next_free_slug(start: int) -> str | None:
        if start < _SLUG_SUFFIX_START and base not in taken:
            return base
        for suffix in range(max(start, _SLUG_SUFFIX_START), _SLUG_SUFFIX_MAX + 1):
            candidate = _fit_suffix(base, suffix)
            if candidate is not None and candidate not in taken:
                return candidate
        return None

    candidate = _next_free_slug(1)
    if candidate is None:
        raise RecipeServiceError("Could not allocate a free tag slug.")

    last_exc: BaseException | None = None
    for _attempt in range(_SLUG_SUFFIX_MAX - _SLUG_SUFFIX_START + 2):
        tag = Tag(name=name, slug=candidate)
        try:
            async with session.begin_nested():
                session.add(tag)
                await session.flush()
            return tag.id
        except IntegrityError as exc:
            if "1062" not in str(getattr(exc, "orig", exc)).lower():
                raise
            last_exc = exc
            rows = await _current_tag_rows(session)
            winner_id = _matching_id(rows)
            if winner_id is not None:
                return winner_id

            taken = {row[2] for row in rows}
            if candidate == base:
                next_suffix = _SLUG_SUFFIX_START
            else:
                suffix = candidate.rpartition("-")[2]
                next_suffix = (
                    max(_SLUG_SUFFIX_START, int(suffix) + 1)
                    if suffix.isdecimal()
                    else _SLUG_SUFFIX_START
                )
            candidate = _next_free_slug(next_suffix)
            if candidate is None:
                raise RecipeServiceError("Could not allocate a free tag slug.") from last_exc
    raise RecipeServiceError("Could not create a tag slug.") from last_exc


async def _create_sections(
    session: AsyncSession, recipe_id: str, save_data: RecipeSaveData
) -> None:
    for position, section in enumerate(save_data.sections, start=1):
        section_row = RecipeSection(
            recipe_id=recipe_id,
            position=position,
            heading=section.heading,
            kind=section.kind,
        )
        session.add(section_row)
        # Need section_row.id for the items below.
        await session.flush()
        for item_position, line in enumerate(section.lines, start=1):
            item = RecipeItem(
                section_id=section_row.id,
                position=item_position,
                raw_text=_line_text(line),
            )
            # Parse columns are stored only for ingredient lines (model
            # contract: defined uniformly, populated on ingredients only).
            if section.kind == "ingredients":
                item.quantity_text = line.quantity_text
                item.unit = line.unit
                item.item = line.item
                item.preparation = line.preparation
            session.add(item)
        await session.flush()


def _revalidate(save_data: RecipeSaveData, category_names: list[str]) -> RecipeSaveData:
    """Re-run the Task 8 save-readiness gate through a reconstructed draft.

    The service does not trust its inputs to have already passed validation —
    any RecipeValidationError is re-raised as :class:`InvalidRecipeInput`
    carrying the same field-error mapping.
    """
    draft = RecipeDraft(
        title=save_data.title,
        description=save_data.description,
        sections=save_data.sections,
        tags=save_data.tags,
        suggested_categories=category_names,
        servings_text=save_data.servings_text,
        servings_number=save_data.servings_number,
        servings_unit=save_data.servings_unit,
        prep_minutes=save_data.prep_minutes,
        cook_minutes=save_data.cook_minutes,
        total_minutes=save_data.total_minutes,
        hero_image=save_data.hero_image,
        provenance=save_data.provenance,
    )
    try:
        return validate_for_save(draft, category_names=category_names)
    except RecipeValidationError as exc:
        raise InvalidRecipeInput(exc.field_errors) from exc


async def _do_create(
    session: AsyncSession,
    slug: str,
    save_data: RecipeSaveData,
    category_rows: list[Category],
    actor_user_id: str | None,
) -> Recipe:
    """Insert the recipe, ordered body, and memberships (no commit)."""
    provenance = save_data.provenance
    recipe = Recipe(
        slug=slug,
        title=save_data.title,
        description=save_data.description,
        servings_text=save_data.servings_text,
        servings_number=save_data.servings_number,
        prep_minutes=save_data.prep_minutes,
        cook_minutes=save_data.cook_minutes,
        total_minutes=save_data.total_minutes,
        # hero_image is deliberately ignored (plan Task 2); image_path unset.
        image_path=None,
        created_by=actor_user_id,
        last_modified_by=actor_user_id,
    )
    if provenance is not None:
        recipe.source_system = provenance.source_system
        recipe.source_id = provenance.source_id
        recipe.source_name = provenance.source_name
        recipe.source_url = provenance.source_url
        recipe.extraction_method = provenance.extraction_method
        recipe.imported_at = provenance.imported_at

    session.add(recipe)
    await session.flush()  # recipe.id is needed for the rows below.

    await _create_sections(session, recipe.id, save_data)

    for category in category_rows:
        await session.execute(
            recipe_categories.insert().values(
                recipe_id=recipe.id, category_id=category.id
            )
        )

    for tag_name in save_data.tags:
        tag_id = await _ensure_tag(session, tag_name)
        await session.execute(
            recipe_tags.insert().values(recipe_id=recipe.id, tag_id=tag_id)
        )

    await session.flush()
    return recipe


async def create_recipe(
    db: AsyncSession,
    data: RecipeSaveData,
    *,
    category_ids: Sequence[str],
    actor_user_id: str | None = None,
    slug: str | None = None,
) -> Recipe:
    """Create and persist a new recipe; returns the ORM object (flush only).

    The caller owns the transaction — this function never commits or rolls
    back the outer transaction. Candidate inserts use nested savepoints
    (``db.begin_nested()``) that roll back locally on failure, so a failed
    create leaves the caller's transaction usable.

    Contract:

    * ``category_ids`` are deduplicated (first-occurrence order); every id
      must exist else :class:`~cookbook.recipes.errors.UnknownCategories`
      retaining the unknown ids.
    * ``data`` is re-validated (Task 8 ``validate_for_save``) against a
      reconstructed draft + the resolved category names; failures raise
      :class:`~cookbook.recipes.errors.InvalidRecipeInput`.
    * Every VARCHAR field the service would write is width-checked against
      the model widths (no truncation); failures raise
      :class:`~cookbook.recipes.errors.InvalidRecipeInput` with every
      offending field.
    * An explicit blank ``slug`` raises
      :class:`~cookbook.recipes.errors.InvalidRecipeInput`; otherwise the
      override slug or the title is slugified (fallback ``recipe``).
    * ``servings_unit`` and ``hero_image`` are ignored; ``image_path`` is
      left unset.
    * Tags are trimmed and deduplicated case-insensitively by first
      occurrence, and missing ones are created in the caller's transaction
      (savepoint-wrapped insert race against concurrent same-name inserts).
    * Final flush before return.
    """
    # --- prechecks (no DB writes) ------------------------------------
    if slug is not None and not slug.strip():
        raise InvalidRecipeInput(
            {"slug": "Slug cannot be blank; pass None to derive it from the title."}
        )
    if actor_user_id is not None and len(actor_user_id) > ACTOR_ID_MAX:
        raise InvalidRecipeInput(
            {
                "actor_user_id": _over_too_long(
                    "actor_user_id", actor_user_id, ACTOR_ID_MAX
                )
            }
        )

    # --- categories: dedupe + resolve to existing rows ---------------
    unique_category_ids: list[str] = []
    seen_ids: set[str] = set()
    for cid in category_ids:
        if cid not in seen_ids:
            seen_ids.add(cid)
            unique_category_ids.append(cid)

    category_rows = list(
        (
            await db.execute(
                select(Category).where(Category.id.in_(unique_category_ids))
            )
        )
        .scalars()
        .all()
    )
    found_ids = {c.id for c in category_rows}
    unknown = [cid for cid in unique_category_ids if cid not in found_ids]
    if unknown:
        raise UnknownCategories(unknown)
    category_rows = [
        next(c for c in category_rows if c.id == cid) for cid in unique_category_ids
    ]

    # --- re-validate against the selected existing category names ----
    save_data = _revalidate(
        data, category_names=[c.name for c in category_rows]
    )

    # --- width checks on everything the service writes --------------
    _check_widths(save_data)

    # --- tags: trim + case-insensitive dedupe by first occurrence ----
    save_data = save_data.model_copy(
        update={"tags": _dedupe_tags_case_insensitive(save_data.tags)}
    )

    # --- slug allocation (pre-check + race-safe insert below) --------
    if slug is not None:
        # Explicit slug: normalize with a max_length high enough to see the
        # FULL normalized result, so an over-width slug is raised (never
        # silently truncated to SLUG_MAX). The check below is what enforces
        # the column width.
        explicit = slugify(slug, fallback="recipe", max_length=SLUG_MAX + 1)
        if len(explicit) > SLUG_MAX:
            raise InvalidRecipeInput(
                {"slug": _over_too_long("slug", explicit, SLUG_MAX)}
            )
        slug_base = explicit
    else:
        # Generated slug: keep the documented cap-at-SLUG_MAX behavior.
        slug_base = slugify(data.title, fallback="recipe", max_length=SLUG_MAX)
    candidate = await _pick_free_slug(db, slug_base) if slug_base else "recipe"

    try:
        async with db.begin_nested():
            return await _do_create(db, candidate, save_data, category_rows, actor_user_id)
    except IntegrityError as exc:
        if not _is_slug_duplicate(exc):
            raise
        # The savepoint rolled back locally: nothing from the failed
        # attempt remains, and the caller's transaction is still active.
        # Someone took our slug between the pre-check and the insert —
        # allocate the next free one and retry (bounded).
        next_suffix = _next_suffix_after_collision(slug_base, candidate)
        for _ in range(_SLUG_SUFFIX_MAX):
            candidate = await _pick_free_slug(
                db,
                slug_base,
                current_read=True,
                start_suffix=next_suffix,
                include_base=False,
            )
            try:
                async with db.begin_nested():
                    return await _do_create(
                        db, candidate, save_data, category_rows, actor_user_id
                    )
            except IntegrityError as retry_exc:
                if not _is_slug_duplicate(retry_exc):
                    raise
                next_suffix = _next_suffix_after_collision(slug_base, candidate)
                continue
        raise RecipeServiceError("Could not allocate a free recipe slug.") from exc


async def _replace_recipe_content(
    db: AsyncSession,
    recipe: Recipe,
    save_data: RecipeSaveData,
    category_rows: list[Category],
    *,
    actor_user_id: str | None,
    new_slug: str,
) -> Recipe:
    """Apply one complete update attempt inside the caller's savepoint."""
    old_slug = recipe.slug
    if new_slug != old_slug:
        new_alias = (
            await db.execute(
                select(RecipeUrlAlias)
                .where(RecipeUrlAlias.path == f"/r/{new_slug}")
            )
        ).scalar_one_or_none()
        if new_alias is not None:
            if new_alias.recipe_id != recipe.id:
                raise RecipeAliasConflict(new_slug)
            # This recipe previously used the target slug. It becomes
            # canonical again, so remove only its own redundant alias row.
            await db.delete(new_alias)

        old_path = f"/r/{old_slug}"
        old_alias = (
            await db.execute(
                select(RecipeUrlAlias)
                .where(RecipeUrlAlias.path == old_path)
            )
        ).scalar_one_or_none()
        if old_alias is not None and old_alias.recipe_id != recipe.id:
            raise RecipeAliasConflict(old_slug)
        if old_alias is None:
            db.add(RecipeUrlAlias(path=old_path, recipe_id=recipe.id))
        recipe.slug = new_slug

    recipe.title = save_data.title
    recipe.description = save_data.description
    recipe.servings_text = save_data.servings_text
    recipe.servings_number = save_data.servings_number
    recipe.prep_minutes = save_data.prep_minutes
    recipe.cook_minutes = save_data.cook_minutes
    recipe.total_minutes = save_data.total_minutes
    if actor_user_id is not None:
        recipe.last_modified_by = actor_user_id

    # Body and memberships are complete replacements. The schema cascades
    # items from sections, so deleting only this recipe's parent sections
    # avoids a correlated child-table delete under MariaDB REPEATABLE READ.
    await db.execute(delete(RecipeSection).where(RecipeSection.recipe_id == recipe.id))
    await db.execute(
        delete(recipe_categories).where(recipe_categories.c.recipe_id == recipe.id)
    )
    await db.execute(
        delete(recipe_tags).where(recipe_tags.c.recipe_id == recipe.id)
    )
    await _create_sections(db, recipe.id, save_data)

    for category in category_rows:
        await db.execute(
            recipe_categories.insert().values(
                recipe_id=recipe.id, category_id=category.id
            )
        )
    for tag_name in save_data.tags:
        tag_id = await _ensure_tag(db, tag_name)
        await db.execute(
            recipe_tags.insert().values(recipe_id=recipe.id, tag_id=tag_id)
        )

    await db.flush()
    return recipe


async def update_recipe(
    db: AsyncSession,
    recipe_id: str,
    data: RecipeSaveData,
    *,
    category_ids: Sequence[str],
    actor_user_id: str | None = None,
    slug: str | None = None,
) -> Recipe:
    """Replace a saved recipe aggregate; flush only, leaving transaction control
    to the caller. A slug change keeps every former route as a URL alias.
    """
    recipe = (
        await db.execute(
            select(Recipe).where(Recipe.id == recipe_id).with_for_update()
        )
    ).scalar_one_or_none()
    if recipe is None:
        raise RecipeNotFound(recipe_id)

    unique_category_ids: list[str] = []
    seen_ids: set[str] = set()
    for category_id in category_ids:
        if category_id not in seen_ids:
            seen_ids.add(category_id)
            unique_category_ids.append(category_id)
    category_rows = list(
        (
            await db.execute(
                select(Category).where(Category.id.in_(unique_category_ids))
            )
        )
        .scalars()
        .all()
    )
    by_id = {category.id: category for category in category_rows}
    unknown = [cid for cid in unique_category_ids if cid not in by_id]
    if unknown:
        raise UnknownCategories(unknown)
    category_rows = [by_id[cid] for cid in unique_category_ids]

    save_data = _revalidate(data, [category.name for category in category_rows])
    _check_widths(save_data)
    save_data = save_data.model_copy(
        update={"tags": _dedupe_tags_case_insensitive(save_data.tags)}
    )

    original_slug = recipe.slug
    if slug is None:
        slug_base = original_slug
    else:
        if not slug.strip():
            raise InvalidRecipeInput({"slug": "Slug must not be blank."})
        slug_base = slugify(slug, fallback="recipe", max_length=SLUG_MAX + 1)
        if len(slug_base) > SLUG_MAX:
            raise InvalidRecipeInput(
                {"slug": f"Slug exceeds the {SLUG_MAX}-character storage limit."}
            )

    def _slug_from_request(candidate_slug: str | None) -> str:
        # A candidate is selected inside each savepoint. A self-alias is
        # removed first, so it can be restored as the canonical route.
        return candidate_slug or original_slug

    async def _clear_requested_self_alias() -> None:
        if slug_base == original_slug:
            return
        alias = (
            await db.execute(
                select(RecipeUrlAlias)
                .where(RecipeUrlAlias.path == f"/r/{slug_base}")
            )
        ).scalar_one_or_none()
        if alias is not None:
            if alias.recipe_id != recipe.id:
                raise RecipeAliasConflict(slug_base)
            await db.delete(alias)

    try:
        async with db.begin_nested():
            await _clear_requested_self_alias()
            candidate = (
                original_slug
                if slug_base == original_slug
                else await _pick_free_slug(db, slug_base)
            )
            return await _replace_recipe_content(
                db,
                recipe,
                save_data,
                category_rows,
                actor_user_id=actor_user_id,
                new_slug=_slug_from_request(candidate),
            )
    except IntegrityError as exc:
        if not _is_slug_duplicate(exc) or slug_base == original_slug:
            raise
        last_exc: BaseException = exc
        next_suffix = _SLUG_SUFFIX_START if candidate == slug_base else _next_suffix_after_collision(slug_base, candidate)

    # A competing update claimed the first candidate after our availability
    # scan. Retry with a current read, rolling back only each update savepoint.
    for _ in range(_SLUG_SUFFIX_MAX):
        try:
            async with db.begin_nested():
                await db.refresh(recipe)
                await _clear_requested_self_alias()
                candidate = await _pick_free_slug(
                    db,
                    slug_base,
                    current_read=True,
                    start_suffix=next_suffix,
                    include_base=False,
                )
                return await _replace_recipe_content(
                    db,
                    recipe,
                    save_data,
                    category_rows,
                    actor_user_id=actor_user_id,
                    new_slug=candidate,
                )
        except IntegrityError as retry_exc:
            if not _is_slug_duplicate(retry_exc):
                raise
            last_exc = retry_exc
            next_suffix = _next_suffix_after_collision(slug_base, candidate)
    raise RecipeServiceError("Could not allocate a free recipe slug.") from last_exc


async def delete_recipe(db: AsyncSession, recipe_id: str) -> None:
    """Delete an unaliased recipe and flush, leaving transaction control to
    the caller. Database cascades remove its body and memberships; aliases
    deliberately restrict deletion so old routes are never orphaned.
    """
    recipe = (
        await db.execute(
            select(Recipe).where(Recipe.id == recipe_id).with_for_update()
        )
    ).scalar_one_or_none()
    if recipe is None:
        raise RecipeNotFound(recipe_id)

    has_alias = (
        await db.execute(
            select(RecipeUrlAlias.id)
            .where(RecipeUrlAlias.recipe_id == recipe_id)
            .limit(1)
        )
    ).first()
    if has_alias is not None:
        raise RecipeHasAliases(recipe_id)

    await db.delete(recipe)
    await db.flush()


async def set_recipe_image(
    db: AsyncSession, recipe_id: str, image_path: str | None
) -> str | None:
    """Set or clear a recipe image path, returning the previous path.

    The caller owns commit/rollback. File validation and disk operations live
    in the image-storage layer; this service only changes the recipe field.
    """
    if image_path is not None and (not image_path or len(image_path) > 500):
        raise InvalidRecipeInput({"image_path": "Image path is empty or too long."})
    recipe = (
        await db.execute(
            select(Recipe).where(Recipe.id == recipe_id).with_for_update()
        )
    ).scalar_one_or_none()
    if recipe is None:
        raise RecipeNotFound(recipe_id)
    previous_path = recipe.image_path
    recipe.image_path = image_path
    await db.flush()
    return previous_path


async def get_recipe(
    db: AsyncSession,
    recipe_id: str,
) -> RecipeDetails | None:
    """Aggregate read for one recipe by immutable id; ``None`` if absent.

    Read-only: no flush and no commit — the session's transaction state is
    untouched either way. All reads are explicit ordered queries (no ORM
    relationships): sections by ``section.position``, items by
    ``item.position`` per section, categories by ``Category.position``,
    tags by ``Tag.name``.
    """
    recipe = (
        (await db.execute(select(Recipe).where(Recipe.id == recipe_id).limit(1)))
        .scalars()
    ).first()
    if recipe is None:
        return None

    sections = (
        (
            await db.execute(
                select(RecipeSection)
                .where(RecipeSection.recipe_id == recipe.id)
                .order_by(RecipeSection.position)
            )
        )
        .scalars()
        .all()
    )

    section_details: list[RecipeSectionDetails] = []
    for section in sections:
        items = (
            (
                await db.execute(
                    select(RecipeItem)
                    .where(RecipeItem.section_id == section.id)
                    .order_by(RecipeItem.position)
                )
            )
            .scalars()
            .all()
        )
        section_details.append(
            RecipeSectionDetails(section=section, items=tuple(items))
        )

    category_ids = (
        (
            await db.execute(
                select(recipe_categories.c.category_id).where(
                    recipe_categories.c.recipe_id == recipe.id
                )
            )
        )
        .scalars()
        .all()
    )
    categories: list[Category] = []
    if category_ids:
        categories = list(
            (
                await db.execute(
                    select(Category)
                    .where(Category.id.in_(category_ids))
                    .order_by(Category.position)
                )
            )
            .scalars()
            .all()
        )

    tag_ids = (
        (
            await db.execute(
                select(recipe_tags.c.tag_id).where(
                    recipe_tags.c.recipe_id == recipe.id
                )
            )
        )
        .scalars()
        .all()
    )
    tags: list[Tag] = []
    if tag_ids:
        tags = list(
            (
                await db.execute(
                    select(Tag)
                    .where(Tag.id.in_(tag_ids))
                    .order_by(Tag.name)
                )
            )
            .scalars()
            .all()
        )

    return RecipeDetails(
        recipe=recipe,
        sections=tuple(section_details),
        categories=tuple(categories),
        tags=tuple(tags),
    )


__all__ = ["create_recipe", "get_recipe"]
