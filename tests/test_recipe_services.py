"""Recipe persistence service tests against the isolated test MariaDB.

These tests cover create, ordered aggregate reads, update/rename, and safe
delete. The services flush only; callers own commit and rollback. The
disposable DB keeps test mutations separate from the running development app.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from cookbook.db import get_engine, get_session
from cookbook.models import (
    Category,
    Recipe,
    RecipeItem,
    RecipeSection,
    RecipeUrlAlias,
    Tag,
    User,
    recipe_categories,
    recipe_tags,
)
from cookbook.recipes import (
    InvalidRecipeInput,
    RecipeAliasConflict,
    RecipeDraft,
    RecipeHasAliases,
    RecipeIngredientDraft,
    RecipeNotFound,
    RecipeProvenanceDraft,
    RecipeSaveData,
    RecipeSectionDraft,
    RecipeStepDraft,
    UnknownCategories,
    create_recipe,
    delete_recipe,
    set_recipe_image,
    update_recipe,
    validate_for_save,
)
from cookbook.recipes.slugs import slugify
import cookbook.recipes.service as recipe_service
# `get_recipe` is planned (plan Task 2, step 6) but NOT implemented or
# exported from `cookbook.recipes` yet — this import is the expected
# failure point for now.
from cookbook.recipes import get_recipe

# Locked UUID of the seeded "Breakfast" category (same seed pinning as
# tests/test_organization.py SEED_ROWS, position 1) — never mutated here.
BREAKFAST_ID = "412b94fd-8106-40b8-863b-46e2b1f1daff"

TITLE = "Task 9.2a Buttermilk Pancakes"
HERO_IMAGE = "images/pancakes.jpg"


def _line_text(line: RecipeIngredientDraft | RecipeStepDraft) -> str:
    """Source text of a draft line, whichever of the two line types it is."""
    if isinstance(line, RecipeIngredientDraft):
        return line.raw_text
    return line.body


def build_save_data(category_name: str) -> RecipeSaveData:
    """A valid draft run through the shared Task 8 draft -> save-data contract."""
    ingredients = RecipeSectionDraft(
        heading="Ingredients",
        kind="ingredients",
        lines=[
            RecipeIngredientDraft(
                raw_text="1 cup buttermilk", quantity_text="1 cup", item="buttermilk"
            ),
            RecipeIngredientDraft(
                raw_text="2 eggs, beaten", item="eggs", preparation="beaten"
            ),
        ],
    )
    steps = RecipeSectionDraft(
        heading="Steps",
        kind="steps",
        lines=[
            RecipeStepDraft(body="Whisk the wet ingredients together."),
            RecipeStepDraft(body="Cook on a griddle until set."),
        ],
    )
    draft = RecipeDraft(
        title=TITLE,
        description="A focused Task 9.2a test recipe.",
        sections=[ingredients, steps],
        tags=[" Brunch", "pancakes"],
        servings_text="serves 4",
        servings_number=Decimal("4"),
        servings_unit="servings",
        prep_minutes=10,
        cook_minutes=12,
        hero_image=HERO_IMAGE,
    )
    return validate_for_save(draft, category_names=[category_name])


async def run() -> None:
    factory = get_session()  # caller-owned session: the service may only flush
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Read one existing seeded category (locked UUID, position 1).
            category = (
                await session.execute(
                    select(Category).where(Category.id == BREAKFAST_ID)
                )
            ).scalar_one()
            assert category.name == "Breakfast", f"seed category name: {category.name!r}"

            # 2) Valid save data via the shared Task 8 contract.
            save_data = build_save_data(category.name)

            # 3) Planned create_recipe signature (plan Task 2, step 4) — flush
            # only; commit/rollback stay with this caller.
            recipe = await create_recipe(session, save_data, category_ids=[category.id])

            # Slug generated from the title (deterministic normalization),
            # title preserved, structured servings + supplied text persisted.
            assert recipe.slug == slugify(TITLE, fallback="recipe"), recipe.slug
            assert recipe.title == save_data.title
            assert recipe.servings_number == Decimal("4")
            assert recipe.servings_text == "serves 4"

            # Hero image must NOT be written to image_path (kept unchanged).
            assert save_data.hero_image == HERO_IMAGE
            assert recipe.image_path is None, f"image_path unexpectedly set: {recipe.image_path!r}"

            # Selected category membership.
            category_ids = (
                await session.execute(
                    select(recipe_categories.c.category_id)
                    .where(recipe_categories.c.recipe_id == recipe.id)
                )
            ).scalars().all()
            assert list(category_ids) == [category.id], list(category_ids)

            # Tag membership, matched by trimmed name (order-agnostic).
            tag_names = (
                await session.execute(
                    select(Tag.name)
                    .join(recipe_tags, recipe_tags.c.tag_id == Tag.id)
                    .where(recipe_tags.c.recipe_id == recipe.id)
                )
            ).scalars().all()
            assert sorted(tag_names) == sorted(save_data.tags), list(tag_names)

            # Ordered sections: same kinds/order, dense positions 1..N.
            stored_sections = (
                await session.execute(
                    select(RecipeSection)
                    .where(RecipeSection.recipe_id == recipe.id)
                    .order_by(RecipeSection.position)
                )
            ).scalars().all()
            assert [s.kind for s in stored_sections] == [s.kind for s in save_data.sections]
            assert (
                [s.position for s in stored_sections]
                == list(range(1, len(save_data.sections) + 1))
            )

            # Ordered items within each section: positions, raw text, and
            # ingredient parse columns only on ingredient lines.
            for stored, expected in zip(stored_sections, save_data.sections, strict=True):
                assert stored.heading == expected.heading
                items = (
                    await session.execute(
                        select(RecipeItem)
                        .where(RecipeItem.section_id == stored.id)
                        .order_by(RecipeItem.position)
                    )
                ).scalars().all()
                assert [i.position for i in items] == list(range(1, len(expected.lines) + 1))
                assert [i.raw_text for i in items] == [_line_text(line) for line in expected.lines]

                if stored.kind == "ingredients":
                    for stored_item, line in zip(items, expected.lines, strict=True):
                        assert isinstance(line, RecipeIngredientDraft)
                        assert stored_item.quantity_text == line.quantity_text
                        assert stored_item.unit == line.unit
                        assert stored_item.item == line.item
                        assert stored_item.preparation == line.preparation
                else:
                    # Steps/note lines carry raw text only — no parse columns.
                    for stored_item in items:
                        assert stored_item.quantity_text is None
                        assert stored_item.unit is None
                        assert stored_item.item is None
                        assert stored_item.preparation is None

            # The service must leave the caller's transaction active (it never
            # commits), so we can still roll everything back below.
            assert session.in_transaction() is True
        finally:
            # Caller-owned rollback: every write from the call above is discarded.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_create_recipe_writes_body_memberships_and_keeps_transaction_active() -> None:
    """Create persists ordered body, category/tag memberships, servings, and a
    generated slug — with image_path untouched — while leaving the caller's
    transaction active for rollback."""
    asyncio.run(run())


async def run_get_recipe() -> None:
    factory = get_session()  # caller-owned session: the service only flushes
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Fetch the first two seeded categories by position — real
            # ordering data that get_recipe must reproduce on read.
            categories = (
                await session.execute(
                    select(Category).order_by(Category.position).limit(2)
                )
            ).scalars().all()
            assert len(categories) == 2
            assert [c.position for c in categories] == [1, 2]

            # 2) Valid save data via the shared Task 8 contract.
            save_data = build_save_data(categories[0].name)

            # 3) create_recipe (implemented) with category IDs intentionally
            # REVERSED relative to category position.
            recipe = await create_recipe(
                session, save_data,
                category_ids=[categories[1].id, categories[0].id],
            )

            # 4) Planned get_recipe (plan Task 2 step 6): aggregate read
            # over the same caller-owned transaction.
            details = await get_recipe(session, recipe.id)

            # Recipe is present with its stored header fields.
            assert details is not None
            assert details.recipe is not None
            assert details.recipe.id == recipe.id
            assert details.recipe.title == save_data.title

            # Sections: a tuple, ordered by position 1..N.
            assert type(details.sections) is tuple
            assert [s.section.position for s in details.sections] == list(
                range(1, len(save_data.sections) + 1)
            )
            assert [s.section.kind for s in details.sections] == [
                s.kind for s in save_data.sections
            ]

            # Per-section items: a tuple, ordered, raw text matching the draft.
            for section_details, expected in zip(details.sections, save_data.sections, strict=True):
                assert type(section_details.items) is tuple
                assert [i.position for i in section_details.items] == list(
                    range(1, len(expected.lines) + 1)
                )
                assert [i.raw_text for i in section_details.items] == [
                    _line_text(line) for line in expected.lines
                ]

            # Categories: a tuple, ordered by Category.position — despite the
            # reversed input order above.
            assert type(details.categories) is tuple
            assert [c.id for c in details.categories] == [c.id for c in categories]
            assert [c.position for c in details.categories] == [1, 2]

            # Tags: a tuple, sorted by tag name.
            assert type(details.tags) is tuple
            assert [t.name for t in details.tags] == sorted(save_data.tags)

            # The service leaves the caller's transaction active.
            assert session.in_transaction() is True

            # 5) An unknown UUID is a quiet miss, not an error (nil UUID:
            # can never be a seeded or created row).
            assert await get_recipe(session, "00000000-0000-0000-0000-000000000000") is None
        finally:
            # Caller-owned rollback: discard every write from the calls above.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_get_recipe_returns_ordered_read_model_for_created_recipe() -> None:
    """Aggregate read: sections and items as ordered tuples, categories by
    position despite reversed input order, tags sorted by name, and None for an
    unknown UUID — all inside one caller-owned transaction that rolls back."""
    asyncio.run(run_get_recipe())


# --- Task 9.2c regression: explicit slug wider than the 200-char column ---

# One char over the Recipe.slug String(200) column width.
OVERWIDE_SLUG = "x" * 201


async def run_explicit_slug_width() -> None:
    factory = get_session()  # caller-owned session: the service may only flush
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Seeded Breakfast category (locked, position 1) — read-only.
            category = (
                await session.execute(
                    select(Category).where(Category.id == BREAKFAST_ID)
                )
            ).scalar_one()
            assert category.name == "Breakfast", f"seed category name: {category.name!r}"

            # 2) Valid save data via the shared Task 8 contract.
            save_data = build_save_data(category.name)

            # 3) An explicit slug wider than the slug column must be
            # REJECTED, not silently truncated to 200 characters.
            with pytest.raises(InvalidRecipeInput) as excinfo:
                await create_recipe(
                    session, save_data, category_ids=[category.id], slug=OVERWIDE_SLUG
                )
            assert "slug" in excinfo.value.field_errors, excinfo.value.field_errors

            # 4) The failed call must not have inserted the recipe.
            recipe_row = (
                await session.execute(select(Recipe).where(Recipe.title == TITLE))
            ).scalar_one_or_none()
            assert recipe_row is None, f"unexpected row: {recipe_row!r}"

            # 5) The caller's transaction must still be active (service never
            # commits and a failed create leaves it usable).
            assert session.in_transaction() is True
        finally:
            # Caller-owned rollback: discard everything from the call above.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_explicit_slug_width_201_raises_not_truncates() -> None:
    """An explicit slug of 201 chars (one over the Recipe.slug String(200)
    column) must raise InvalidRecipeInput with a ``slug`` field error, insert
    no recipe, and leave the caller's transaction usable — catching silent
    truncation by slugify(max_length=200)."""
    asyncio.run(run_explicit_slug_width())


# --- Task 9.2d (test-only): route reservation + tag reuse/slug suffixing ---

# Two explicitly-requested slugs: one already owned by a recipe row, one owned
# only as a legacy alias path. Both must be treated as "in use" so the new
# recipes land on the ``-2`` variants and the existing rows stay untouched.
EXISTING_RECIPE_SLUG = "task9-existing-recipe-slug"
EXISTING_ALIAS_SLUG = "task9-existing-alias-slug"


async def run_slug_route_reservation() -> None:
    factory = get_session()  # caller-owned session: the service may only flush
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Seed the two "route already taken" fixtures:
            #    (a) a real recipe occupying the recipe slug,
            #    (b) a recipe + an alias whose path is /r/<slug>.
            recipe_a = Recipe(slug=EXISTING_RECIPE_SLUG, title="Task 9.2d Existing A")
            session.add(recipe_a)
            await session.flush()

            recipe_target = Recipe(slug="task9-9-2d-alias-target", title="Task 9.2d Alias Target")
            session.add(recipe_target)
            await session.flush()

            alias = RecipeUrlAlias(path=f"/r/{EXISTING_ALIAS_SLUG}", recipe_id=recipe_target.id)
            session.add(alias)
            await session.flush()

            # 2) Seeded Breakfast category (locked, position 1) — read-only.
            category = (
                await session.execute(
                    select(Category).where(Category.id == BREAKFAST_ID)
                )
            ).scalar_one()
            assert category.name == "Breakfast", f"seed category name: {category.name!r}"

            # 3) Valid save data via the shared Task 8 contract.
            save_data = build_save_data(category.name)

            # 4) First create: explicit slug equal to the existing recipe row
            #    slug -> must be reserved and re-suffixed to ``-2``.
            recipe_one = await create_recipe(
                session, save_data,
                category_ids=[category.id], slug=EXISTING_RECIPE_SLUG,
            )
            assert recipe_one.slug == f"{EXISTING_RECIPE_SLUG}-2", recipe_one.slug

            # 5) Second create: explicit slug owned only by a legacy alias
            #    path (/r/<slug>) -> also reserved, re-suffixed to ``-2``.
            recipe_two = await create_recipe(
                session, save_data,
                category_ids=[category.id], slug=EXISTING_ALIAS_SLUG,
            )
            assert recipe_two.slug == f"{EXISTING_ALIAS_SLUG}-2", recipe_two.slug

            # 6) The existing recipe slug and alias path are unchanged —
            #    reservation never mutates the rows that blocked it.
            assert recipe_a.slug == EXISTING_RECIPE_SLUG, recipe_a.slug
            alias_paths = (
                await session.execute(select(RecipeUrlAlias.path))
            ).scalars().all()
            assert f"/r/{EXISTING_ALIAS_SLUG}" in alias_paths, list(alias_paths)
            assert f"/r/{EXISTING_ALIAS_SLUG}-2" not in alias_paths, list(alias_paths)

            # Distinct new recipes; neither stole the other's suffix slot.
            assert recipe_one.id != recipe_two.id
            assert {recipe_one.slug, recipe_two.slug} == {
                f"{EXISTING_RECIPE_SLUG}-2",
                f"{EXISTING_ALIAS_SLUG}-2",
            }

            # The caller's transaction must still be active.
            assert session.in_transaction() is True
        finally:
            # Caller-owned rollback: discard every write from the calls above.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_explicit_slug_reserves_recipe_slug_and_alias_path() -> None:
    """An explicit slug already owned by a recipe row, or owned only as a
    legacy alias path (/r/<slug>), must be treated as reserved: each new
    recipe lands on the ``-2`` variant while the blocking recipe/alias rows are
    left untouched — pinning route reservation for recipe slugs and alias
    paths."""
    asyncio.run(run_slug_route_reservation())


# Preinserted tag rows (names intentionally messy) and the three tag names the
# create call will be handed. The contract under pin: the two BRUNCH variants
# must reuse ONE existing tag (matched by stored name after trim + casefold,
# not by raw input case/whitespace), while tomato+soup is a NEW tag that gets
# its own slug ``tomato-soup-2`` (not conflated with the existing tomato-soup).
BRUNCH_PADDED_NAME = "  BRUNCH  "
BRUNCH_SLUG = "task9-legacy-brunch"
TOMATO_SOUP_NAME = "tomato soup"
TOMATO_SOUP_SLUG = "tomato-soup"

TAG_INPUTS = [" brunch ", "BRUNCH", "tomato+soup"]


async def run_tag_reuse_and_slug() -> None:
    factory = get_session()  # caller-owned session: the service may only flush
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Seed two distinct tags with colliding-but-distinct names.
            brunch_tag = Tag(name=BRUNCH_PADDED_NAME, slug=BRUNCH_SLUG)
            session.add(brunch_tag)
            tomato_soup_tag = Tag(name=TOMATO_SOUP_NAME, slug=TOMATO_SOUP_SLUG)
            session.add(tomato_soup_tag)
            await session.flush()

            # 2) Seeded Breakfast category (locked, position 1) — read-only.
            category = (
                await session.execute(
                    select(Category).where(Category.id == BREAKFAST_ID)
                )
            ).scalar_one()
            assert category.name == "Breakfast", f"seed category name: {category.name!r}"

            # 3) Saved data with messy tag inputs (trim + casefold must
            #    collapse the two BRUNCH variants into one tag).
            save_data = build_save_data(category.name).model_copy(
                update={"tags": TAG_INPUTS}
            )

            # 4) Create: two BRUNCH inputs reuse the existing padded tag;
            #    tomato+soup creates a new, distinct tag.
            recipe = await create_recipe(
                session, save_data,
                category_ids=[category.id], slug="task9-9-2d-tag-reuse",
            )

            # 5) The recipe is tagged exactly twice: the existing BRUNCH tag
            #    (reused) + the NEW tomato+soup tag. No duplicate, no
            #    conflation with the existing tomato-soup row.
            recipe_tag_rows = (
                await session.execute(
                    select(Recipe, Tag)
                    .join(recipe_tags, recipe_tags.c.recipe_id == Recipe.id)
                    .join(Tag, Tag.id == recipe_tags.c.tag_id)
                    .where(Recipe.id == recipe.id)
                )
            ).all()
            recipe_tag_ids = {tag_row.id for _, tag_row in recipe_tag_rows}
            assert len(recipe_tag_ids) == 2, [t.id for t in recipe_tag_ids]

            # BRUNCH: membership reuses the preinserted padded/case tag id.
            assert brunch_tag.id in recipe_tag_ids, (
                f"BRUNCH variants failed to reuse the existing tag; "
                f"recipe tags = {sorted(recipe_tag_ids)}"
            )

            # The existing tomato-soup tag must NOT be linked (no conflation).
            assert tomato_soup_tag.id not in recipe_tag_ids, (
                f"tomato+soup conflated with the existing {TOMATO_SOUP_NAME!r} tag"
            )

            # 6) The new tomato+soup tag is a distinct row whose name is the
            #    trimmed input and whose slug is the suffixed ``tomato-soup-2``
            #    (not the taken ``tomato-soup`` base).
            new_tag_ids = recipe_tag_ids - {brunch_tag.id, tomato_soup_tag.id}
            assert len(new_tag_ids) == 1, list(new_tag_ids)
            new_tag = (
                await session.execute(
                    select(Tag).where(Tag.id == next(iter(new_tag_ids)))
                )
            ).scalar_one()
            assert new_tag.name == "tomato+soup", new_tag.name
            assert new_tag.slug == "tomato-soup-2", new_tag.slug

            # 7) The existing BRUNCH tag is unchanged (reuse, not rewrite).
            assert brunch_tag.slug == BRUNCH_SLUG, brunch_tag.slug

            # 8) The caller's transaction must still be active.
            assert session.in_transaction() is True
        finally:
            # Caller-owned rollback: discard every write from the calls above.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_tag_reuse_trim_casefold_and_slug_suffix() -> None:
    """Tag membership must reuse one existing tag across trim/case variants
    (matched by stored name after trim + casefold) and, when a new tag is
    created, give it a distinct suffixed slug (``tomato-soup-2``) rather than
    conflating a look-alike with the existing ``tomato soup`` row — pinning
    trim/case-insensitive tag reuse and tag slug suffixing."""
    asyncio.run(run_tag_reuse_and_slug())


# --- Task 9.2e (test-only): category id validation edge cases ---

# A well-formed UUID that can never be a seeded or created row (nil UUID).
MISSING_CATEGORY_ID = "00000000-0000-0000-0000-000000000000"


async def run_category_validation() -> None:
    factory = get_session()  # caller-owned session: the service may only flush
    session = factory()
    try:
        await session.begin()
        try:
            # 1) Duplicate category IDs: [BREAKFAST_ID, BREAKFAST_ID] must
            #    produce exactly ONE association row (de-duplicated).
            save_data = build_save_data("Breakfast")
            recipe = await create_recipe(
                session, save_data, category_ids=[BREAKFAST_ID, BREAKFAST_ID]
            )
            association_rows = (
                await session.execute(
                    select(recipe_categories)
                    .where(recipe_categories.c.recipe_id == recipe.id)
                )
            ).all()
            assert len(association_rows) == 1, len(association_rows)
            assert [row.category_id for row in association_rows] == [BREAKFAST_ID]

            # 2) One nonexistent UUID: UnknownCategories must retain exactly
            #    that ID, no extra recipe may be inserted, and the caller's
            #    transaction must remain active.
            pre_row_count = (
                await session.execute(select(Recipe).where(Recipe.title == TITLE))
            ).scalars().all()
            with pytest.raises(UnknownCategories) as excinfo:
                await create_recipe(
                    session, save_data, category_ids=[MISSING_CATEGORY_ID]
                )
            assert excinfo.value.unknown_ids == [MISSING_CATEGORY_ID], excinfo.value.unknown_ids
            post_row_count = (
                await session.execute(select(Recipe).where(Recipe.title == TITLE))
            ).scalars().all()
            assert len(post_row_count) == len(pre_row_count), "extra recipe inserted"
            assert session.in_transaction() is True

            # 3) Empty category IDs: an input error (InvalidRecipeInput with a
            #    ``categories`` field error), no extra recipe, outer
            #    transaction still active.
            with pytest.raises(InvalidRecipeInput) as excinfo:
                await create_recipe(session, save_data, category_ids=[])
            assert "categories" in excinfo.value.field_errors, excinfo.value.field_errors
            final_row_count = (
                await session.execute(select(Recipe).where(Recipe.title == TITLE))
            ).scalars().all()
            assert len(final_row_count) == len(pre_row_count), "extra recipe inserted"
            assert session.in_transaction() is True
        finally:
            # Caller-owned rollback: discard every write from the calls above.
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()  # drain the process-global pool before the next loop


def test_category_ids_duplicate_unknown_and_empty() -> None:
    """Category id validation in one caller-owned transaction: duplicate ids
    collapse to exactly one association row; one unknown id raises
    UnknownCategories retaining exactly that id with no extra recipe and the
    outer transaction still active; an empty id list raises InvalidRecipeInput
    with a ``categories`` field error, again no extra recipe and an active
    transaction."""
    asyncio.run(run_category_validation())


CONCURRENT_RECIPE_SLUG = "task9-concurrent-create"


def test_concurrent_create_retries_same_slug_inside_outer_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force two callers past the same free-slug check before either inserts.

    One insert wins; the other must recover from the unique-key race in a
    savepoint, allocate the next slug, and leave both caller transactions
    usable. The suffix limit is kept small so a stale-read retry fails fast.
    """
    async def run() -> None:
        engine = get_engine()
        factory = get_session()
        barrier = asyncio.Barrier(2)
        original_pick = recipe_service._pick_free_slug
        synchronized_calls = 0

        async def synchronized_pick(session, base: str, **kwargs) -> str:
            nonlocal synchronized_calls
            candidate = await original_pick(session, base, **kwargs)
            if (
                base == CONCURRENT_RECIPE_SLUG
                and not kwargs
                and synchronized_calls < 2
            ):
                synchronized_calls += 1
                await barrier.wait()
            return candidate

        monkeypatch.setattr(recipe_service, "_pick_free_slug", synchronized_pick)

        async def create_one() -> str:
            async with factory() as session:
                async with session.begin():
                    category = await session.get(Category, BREAKFAST_ID)
                    assert category is not None
                    data = build_save_data(category.name).model_copy(update={"tags": []})
                    recipe = await create_recipe(
                        session,
                        data,
                        category_ids=[category.id],
                        slug=CONCURRENT_RECIPE_SLUG,
                    )
                    assert session.in_transaction()
                    return recipe.slug

        try:
            results = await asyncio.gather(
                create_one(), create_one(), return_exceptions=True
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            slugs = {result for result in results if isinstance(result, str)}
            assert slugs == {
                CONCURRENT_RECIPE_SLUG,
                f"{CONCURRENT_RECIPE_SLUG}-2",
            }, slugs

        finally:
            # Each successful session commits. Remove only these test rows.
            async with factory() as cleanup:
                await cleanup.execute(
                    delete(Recipe).where(
                        Recipe.slug.in_(
                            [CONCURRENT_RECIPE_SLUG, f"{CONCURRENT_RECIPE_SLUG}-2"]
                        )
                    )
                )
                await cleanup.commit()
            await engine.dispose()

    monkeypatch.setattr(recipe_service, "_SLUG_SUFFIX_MAX", 4)
    asyncio.run(run())


async def run_creation_width_validation() -> None:
    factory = get_session()
    session = factory()
    try:
        await session.begin()
        try:
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            valid = build_save_data(category.name)
            ingredient_section = valid.sections[0]
            ingredient_line = ingredient_section.lines[0]

            invalid_inputs = [
                (valid, "slug", "   "),
                (valid.model_copy(update={"title": "t" * 301}), "title", None),
                (
                    valid.model_copy(update={"servings_text": "s" * 101}),
                    "servings_text",
                    None,
                ),
                (
                    valid.model_copy(update={"tags": ["tag" * 34]}),
                    "tags[0]",
                    None,
                ),
                (
                    valid.model_copy(
                        update={
                            "sections": [
                                ingredient_section.model_copy(
                                    update={"heading": "h" * 201}
                                ),
                                *valid.sections[1:],
                            ]
                        }
                    ),
                    "sections[0].heading",
                    None,
                ),
                (
                    valid.model_copy(
                        update={
                            "sections": [
                                ingredient_section.model_copy(
                                    update={
                                        "lines": [
                                            ingredient_line.model_copy(
                                                update={"quantity_text": "q" * 51}
                                            ),
                                            *ingredient_section.lines[1:],
                                        ]
                                    }
                                ),
                                *valid.sections[1:],
                            ]
                        }
                    ),
                    "sections[0].lines[0].quantity_text",
                    None,
                ),
            ]

            for data, expected_field, requested_slug in invalid_inputs:
                with pytest.raises(InvalidRecipeInput) as raised:
                    await create_recipe(
                        session,
                        data,
                        category_ids=[category.id],
                        slug=requested_slug,
                    )
                assert expected_field in raised.value.field_errors, raised.value.field_errors
                assert session.in_transaction()

            recipes = (
                await session.execute(select(Recipe).where(Recipe.title == TITLE))
            ).scalars().all()
            assert recipes == []
        finally:
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()


def test_create_rejects_blank_slug_and_model_width_overflows() -> None:
    asyncio.run(run_creation_width_validation())


async def run_actor_and_provenance_mapping() -> None:
    factory = get_session()
    session = factory()
    try:
        await session.begin()
        try:
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            actor = User(
                username=f"task9-actor-{uuid.uuid4().hex}",
                password_hash="test-only-not-a-real-hash",
                display_name="Task 9 test editor",
            )
            session.add(actor)
            await session.flush()

            imported_at = datetime(2026, 10, 30, 12, 0, 0)
            provenance = RecipeProvenanceDraft(
                source_system="wordpress",
                source_id="task9-source-1869",
                source_url="https://example.test/imported-recipe",
                source_name="Example Kitchen",
                extraction_method="wxr",
                imported_at=imported_at,
            )
            data = build_save_data(category.name).model_copy(
                update={"provenance": provenance}
            )

            recipe = await create_recipe(
                session,
                data,
                category_ids=[category.id],
                actor_user_id=actor.id,
            )
            assert recipe.created_by == actor.id
            assert recipe.last_modified_by == actor.id
            assert recipe.source_system == "wordpress"
            assert recipe.source_id == "task9-source-1869"
            assert recipe.source_url == "https://example.test/imported-recipe"
            assert recipe.source_name == "Example Kitchen"
            assert recipe.extraction_method == "wxr"
            assert recipe.imported_at == imported_at
            assert session.in_transaction()
        finally:
            await session.rollback()
    finally:
        await session.close()
        await get_engine().dispose()


def test_create_maps_actor_and_import_provenance() -> None:
    asyncio.run(run_actor_and_provenance_mapping())


def replacement_data(category_name: str, title: str, tags: list[str]) -> RecipeSaveData:
    draft = RecipeDraft(
        title=title,
        description="Replacement description",
        sections=[
            RecipeSectionDraft(
                heading="New ingredients",
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text="3 pears", item="pears")],
            ),
            RecipeSectionDraft(
                heading="New method",
                kind="steps",
                lines=[RecipeStepDraft(body="Bake the pears slowly.")],
            ),
        ],
        tags=tags,
        servings_text="makes 6 portions",
        servings_number=Decimal("6"),
        servings_unit="portions",
        prep_minutes=7,
        cook_minutes=25,
        total_minutes=32,
        provenance=RecipeProvenanceDraft(
            source_system="changed-source",
            source_id="changed-id",
            source_name="Changed source",
            source_url="https://example.test/changed",
            extraction_method="manual",
        ),
    )
    return validate_for_save(draft, category_names=[category_name])


async def run_update_replacement_and_aliases() -> None:
    factory = get_session()
    token = uuid.uuid4().hex[:8]
    original_slug = f"task9-update-original-{token}"
    second_slug = f"task9-update-v2-{token}"
    third_slug = f"task9-update-v3-{token}"
    initial_tags = [f"task9-start-{token}", f"task9-start-two-{token}"]
    replacement_tags = [f"task9-weeknight-{token}", f"task9-pear-{token}"]
    async with factory() as session:
        async with session.begin():
            categories = (
                await session.execute(select(Category).order_by(Category.position).limit(2))
            ).scalars().all()
            assert len(categories) == 2
            provenance = RecipeProvenanceDraft(
                source_system="original-source",
                source_id=f"original-{token}",
                source_name="Original source",
                source_url="https://example.test/original",
                extraction_method="jsonld",
                imported_at=datetime(2024, 1, 2, 3, 4),
            )
            initial = build_save_data(categories[0].name).model_copy(
                update={
                    "title": "Task 9 Update Original",
                    "provenance": provenance,
                    "tags": initial_tags,
                }
            )
            recipe = await create_recipe(
                session, initial, category_ids=[categories[0].id], slug=original_slug
            )
            actor = User(
                username=f"task9-update-editor-{token}",
                password_hash="test-only-not-a-real-hash",
                display_name="Task 9 update editor",
            )
            session.add(actor)
            await session.flush()
            recipe.image_path = "stored/immutable-image.jpg"
            recipe.published_date = date(2024, 2, 3)
            await session.flush()
            recipe_id = recipe.id

            title_only = build_save_data(categories[0].name).model_copy(
                update={"title": "Task 9 Update Title Only", "tags": initial_tags}
            )
            current = await update_recipe(
                session,
                recipe_id,
                title_only,
                category_ids=[categories[0].id],
                actor_user_id=actor.id,
            )
            assert current.slug == original_slug
            assert current.last_modified_by == actor.id
            assert session.in_transaction()

            replacement = replacement_data(
                categories[1].name, "Task 9 Update Renamed", replacement_tags
            )
            current = await update_recipe(
                session,
                recipe_id,
                replacement,
                category_ids=[categories[1].id],
                slug=second_slug,
            )
            assert current.slug == second_slug
            replacement = replacement_data(
                categories[1].name, "Task 9 Update Renamed Again", [replacement_tags[0]]
            )
            current = await update_recipe(
                session, recipe_id, replacement, category_ids=[categories[1].id], slug=third_slug
            )
            assert current.slug == third_slug

            # Returning to a path this recipe already owns as an alias restores
            # it as the canonical slug and removes only that self-alias.
            replacement = replacement_data(
                categories[1].name, "Task 9 Update Back", [replacement_tags[0]]
            )
            current = await update_recipe(
                session,
                recipe_id,
                replacement,
                category_ids=[categories[1].id],
                slug=original_slug,
            )
            assert current.slug == original_slug

            aliases = (
                await session.execute(
                    select(RecipeUrlAlias.path, RecipeUrlAlias.recipe_id).where(
                        RecipeUrlAlias.recipe_id == recipe_id
                    )
                )
            ).all()
            assert {path for path, _ in aliases} == {f"/r/{second_slug}", f"/r/{third_slug}"}

            sections = (
                await session.execute(
                    select(RecipeSection).where(RecipeSection.recipe_id == recipe_id).order_by(RecipeSection.position)
                )
            ).scalars().all()
            assert [section.heading for section in sections] == ["New ingredients", "New method"]
            items = (
                await session.execute(
                    select(RecipeItem)
                    .join(RecipeSection, RecipeSection.id == RecipeItem.section_id)
                    .where(RecipeItem.section_id.in_([s.id for s in sections]))
                    .order_by(RecipeSection.position, RecipeItem.position)
                )
            ).scalars().all()
            assert [item.raw_text for item in items] == ["3 pears", "Bake the pears slowly."]
            category_ids = (
                await session.execute(
                    select(recipe_categories.c.category_id).where(recipe_categories.c.recipe_id == recipe_id)
                )
            ).scalars().all()
            assert category_ids == [categories[1].id]
            tag_names = (
                await session.execute(
                    select(Tag.name).join(recipe_tags, recipe_tags.c.tag_id == Tag.id).where(recipe_tags.c.recipe_id == recipe_id).order_by(Tag.name)
                )
            ).scalars().all()
            assert tag_names == [replacement_tags[0]]
            assert current.servings_number == Decimal("6")
            assert current.servings_text == "makes 6 portions"
            assert current.source_system == "original-source"
            assert current.source_id == f"original-{token}"
            assert current.source_name == "Original source"
            assert current.source_url == "https://example.test/original"
            assert current.extraction_method == "jsonld"
            assert current.imported_at == datetime(2024, 1, 2, 3, 4)
            assert current.image_path == "stored/immutable-image.jpg"
            assert current.published_date == date(2024, 2, 3)
            assert current.last_modified_by == actor.id
            tag_ids = (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
                )
            ).scalars().all()
            managed_tag_ids = (
                await session.execute(
                    select(Tag.id).where(Tag.name.in_([*initial_tags, *replacement_tags]))
                )
            ).scalars().all()
            await session.execute(
                delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id == recipe_id)
            )
            await session.execute(delete(Recipe).where(Recipe.id == recipe_id))
            await session.execute(
                delete(Tag).where(Tag.id.in_(set(managed_tag_ids) | set(tag_ids)))
            )
            await session.execute(delete(User).where(User.id == actor.id))
    await get_engine().dispose()


def test_update_replaces_save_data_and_preserves_rename_aliases() -> None:
    asyncio.run(run_update_replacement_and_aliases())


async def run_update_conflict_and_missing() -> None:
    factory = get_session()
    token = uuid.uuid4().hex[:8]
    source_slug = f"task9-conflict-source-{token}"
    target_slug = f"task9-conflict-target-{token}"
    reserved_slug = f"task9-reserved-alias-{token}"
    async with factory() as session:
        async with session.begin():
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            source = await create_recipe(
                session,
                build_save_data(category.name).model_copy(
                    update={
                        "title": "Task 9 Conflict Source",
                        "tags": [f"task9-conflict-source-tag-{token}"],
                    }
                ),
                category_ids=[category.id],
                slug=source_slug,
            )
            target = await create_recipe(
                session,
                build_save_data(category.name).model_copy(
                    update={
                        "title": "Task 9 Conflict Target",
                        "tags": [f"task9-conflict-target-tag-{token}"],
                    }
                ),
                category_ids=[category.id],
                slug=target_slug,
            )
            session.add(RecipeUrlAlias(path=f"/r/{reserved_slug}", recipe_id=target.id))
            await session.flush()
            before_title = source.title
            with pytest.raises(RecipeAliasConflict):
                await update_recipe(
                    session,
                    source.id,
                    replacement_data(
                        category.name,
                        "Should not persist",
                        [f"task9-no-persist-tag-{token}"],
                    ),
                    category_ids=[category.id],
                    slug=reserved_slug,
                )
            assert source.title == before_title
            assert source.slug == source_slug
            with pytest.raises(RecipeNotFound):
                await update_recipe(
                    session,
                    "missing-task9-recipe-id",
                    build_save_data(category.name),
                    category_ids=[category.id],
                )
            with pytest.raises(UnknownCategories):
                await update_recipe(
                    session,
                    source.id,
                    build_save_data(category.name),
                    category_ids=["missing-task9-category-id"],
                )
            tag_ids = (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(
                        recipe_tags.c.recipe_id.in_([source.id, target.id])
                    )
                )
            ).scalars().all()
            await session.execute(
                delete(RecipeUrlAlias).where(
                    RecipeUrlAlias.recipe_id.in_([source.id, target.id])
                )
            )
            await session.execute(
                delete(Recipe).where(Recipe.id.in_([source.id, target.id]))
            )
            await session.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
    await get_engine().dispose()


def test_update_conflicts_and_missing_references_are_reported() -> None:
    asyncio.run(run_update_conflict_and_missing())


def test_concurrent_updates_allocate_distinct_slugs_and_preserve_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_concurrent_renames() -> None:
        engine = get_engine()
        factory = get_session()
        token = uuid.uuid4().hex[:8]
        original_slugs = [f"task9-rename-a-{token}", f"task9-rename-b-{token}"]
        wanted_slug = f"task9-concurrent-rename-{token}"
        async with factory() as setup:
            async with setup.begin():
                category = await setup.get(Category, BREAKFAST_ID)
                assert category is not None
                recipes = [
                    await create_recipe(
                        setup,
                        build_save_data(category.name).model_copy(
                            update={"title": f"Task 9 rename setup {index}", "tags": []}
                        ),
                        category_ids=[category.id],
                        slug=old_slug,
                    )
                    for index, old_slug in enumerate(original_slugs)
                ]
                recipe_ids = [recipe.id for recipe in recipes]

        barrier = asyncio.Barrier(2)
        original_pick = recipe_service._pick_free_slug
        synchronized_calls = 0

        async def synchronized_pick(session, base: str, **kwargs) -> str:
            nonlocal synchronized_calls
            candidate = await original_pick(session, base, **kwargs)
            if base == wanted_slug and not kwargs and synchronized_calls < 2:
                synchronized_calls += 1
                await barrier.wait()
            return candidate

        monkeypatch.setattr(recipe_service, "_pick_free_slug", synchronized_pick)

        async def rename_one(index: int) -> tuple[str, str]:
            async with factory() as session:
                async with session.begin():
                    category = await session.get(Category, BREAKFAST_ID)
                    assert category is not None
                    data = replacement_data(
                        category.name, f"Task 9 concurrent rename {index}", []
                    )
                    recipe = await update_recipe(
                        session,
                        recipe_ids[index],
                        data,
                        category_ids=[category.id],
                        slug=wanted_slug,
                    )
                    assert session.in_transaction()
                    return recipe.id, recipe.slug

        try:
            results = await asyncio.gather(rename_one(0), rename_one(1))
            assert {slug for _, slug in results} == {wanted_slug, f"{wanted_slug}-2"}
            async with factory() as verify:
                aliases = (
                    await verify.execute(
                        select(RecipeUrlAlias.path, RecipeUrlAlias.recipe_id).where(
                            RecipeUrlAlias.recipe_id.in_(recipe_ids)
                        )
                    )
                ).all()
                assert {path for path, _ in aliases} == {
                    f"/r/{old_slug}" for old_slug in original_slugs
                }
        finally:
            async with factory() as cleanup:
                tag_ids = (
                    await cleanup.execute(
                        select(recipe_tags.c.tag_id).where(
                            recipe_tags.c.recipe_id.in_(recipe_ids)
                        )
                    )
                ).scalars().all()
                await cleanup.execute(
                    delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id.in_(recipe_ids))
                )
                await cleanup.execute(delete(Recipe).where(Recipe.id.in_(recipe_ids)))
                await cleanup.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
                await cleanup.commit()
            await engine.dispose()

    monkeypatch.setattr(recipe_service, "_SLUG_SUFFIX_MAX", 4)
    asyncio.run(run_concurrent_renames())


async def run_delete_cascades_memberships() -> None:
    factory = get_session()
    token = uuid.uuid4().hex[:8]
    async with factory() as session:
        async with session.begin():
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            recipe = await create_recipe(
                session,
                build_save_data(category.name).model_copy(
                    update={"title": f"Task 9 delete cascade {token}"}
                ),
                category_ids=[category.id],
                slug=f"task9-delete-cascade-{token}",
            )
            recipe_id = recipe.id
            tag_ids = (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
                )
            ).scalars().all()
            tag_ids = (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
                )
            ).scalars().all()

    async with factory() as session:
        async with session.begin():
            await delete_recipe(session, recipe_id)
            assert session.in_transaction()
            assert await session.get(Recipe, recipe_id) is None
            assert not (
                await session.execute(
                    select(RecipeSection.id).where(RecipeSection.recipe_id == recipe_id)
                )
            ).scalars().all()
            assert not (
                await session.execute(
                    select(recipe_categories.c.category_id).where(
                        recipe_categories.c.recipe_id == recipe_id
                    )
                )
            ).scalars().all()
            assert not (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
                )
            ).scalars().all()
            remaining_tags = (
                await session.execute(select(Tag.id).where(Tag.id.in_(tag_ids)))
            ).scalars().all()
            assert set(remaining_tags) == set(tag_ids)
            await session.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
    await get_engine().dispose()


def test_delete_cascades_recipe_body_and_memberships_but_keeps_tags() -> None:
    asyncio.run(run_delete_cascades_memberships())


async def run_delete_alias_guard() -> None:
    factory = get_session()
    token = uuid.uuid4().hex[:8]
    async with factory() as session:
        async with session.begin():
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            recipe = await create_recipe(
                session,
                build_save_data(category.name).model_copy(
                    update={"title": f"Task 9 delete alias guard {token}"}
                ),
                category_ids=[category.id],
                slug=f"task9-delete-alias-guard-{token}",
            )
            recipe_id = recipe.id
            alias = RecipeUrlAlias(path=f"/r/task9-delete-guard-{token}", recipe_id=recipe_id)
            session.add(alias)
            await session.flush()
            with pytest.raises(RecipeHasAliases):
                await delete_recipe(session, recipe_id)
            assert await session.get(Recipe, recipe_id) is not None
            assert await session.get(RecipeSection, (await session.execute(
                select(RecipeSection.id).where(RecipeSection.recipe_id == recipe_id).limit(1)
            )).scalar_one()) is not None
            assert await session.get(RecipeUrlAlias, alias.id) is not None
            tag_ids = (
                await session.execute(
                    select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
                )
            ).scalars().all()
            await session.execute(
                delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id == recipe_id)
            )
            await session.execute(delete(Recipe).where(Recipe.id == recipe_id))
            await session.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
    await get_engine().dispose()


def test_delete_rejects_aliased_recipe_without_partial_changes() -> None:
    asyncio.run(run_delete_alias_guard())


async def run_delete_missing_and_outer_rollback() -> None:
    factory = get_session()
    token = uuid.uuid4().hex[:8]
    async with factory() as session:
        async with session.begin():
            with pytest.raises(RecipeNotFound):
                await delete_recipe(session, f"missing-delete-{token}")

    async with factory() as session:
        async with session.begin():
            category = await session.get(Category, BREAKFAST_ID)
            assert category is not None
            recipe = await create_recipe(
                session,
                build_save_data(category.name).model_copy(
                    update={"title": f"Task 9 delete rollback {token}"}
                ),
                category_ids=[category.id],
                slug=f"task9-delete-rollback-{token}",
            )
            recipe_id = recipe.id

    async with factory() as session:
        async with session.begin():
            await delete_recipe(session, recipe_id)
            assert await session.get(Recipe, recipe_id) is None
            await session.rollback()

    async with factory() as verify:
        assert await verify.get(Recipe, recipe_id) is not None
        tag_ids = (
            await verify.execute(
                select(recipe_tags.c.tag_id).where(recipe_tags.c.recipe_id == recipe_id)
            )
        ).scalars().all()
    async with factory() as cleanup:
        async with cleanup.begin():
            await cleanup.execute(delete(Recipe).where(Recipe.id == recipe_id))
            await cleanup.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
    await get_engine().dispose()


def test_delete_reports_missing_and_respects_caller_rollback() -> None:
    asyncio.run(run_delete_missing_and_outer_rollback())


async def run_set_recipe_image_is_caller_managed() -> None:
    factory = get_session()
    session = factory()
    recipe_id: str | None = None
    try:
        await session.begin()
        category = (
            await session.execute(select(Category).where(Category.id == BREAKFAST_ID))
        ).scalar_one()
        recipe = await create_recipe(
            session, build_save_data(category.name), category_ids=[category.id]
        )
        recipe_id = recipe.id

        previous = await set_recipe_image(session, recipe_id, "recipes/old.png")
        assert previous is None
        previous = await set_recipe_image(session, recipe_id, "recipes/new.webp")
        assert previous == "recipes/old.png"
        assert recipe.image_path == "recipes/new.webp"
        assert recipe.title == TITLE
        assert session.in_transaction() is True

        with pytest.raises(RecipeNotFound):
            await set_recipe_image(session, str(uuid.uuid4()), "recipes/missing.png")
        await session.rollback()
        async with factory() as verify:
            assert await verify.get(Recipe, recipe_id) is None
    finally:
        await session.close()
        await get_engine().dispose()


def test_set_recipe_image_only_changes_image_and_leaves_transaction_to_caller() -> None:
    asyncio.run(run_set_recipe_image_is_caller_managed())
