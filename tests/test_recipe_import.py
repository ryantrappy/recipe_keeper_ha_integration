"""Recipe parsing and public-only fetch boundaries, with no real network calls."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from recipe_assistant.recipe_import import (
    MAX_PAGE_BYTES,
    PublicResolver,
    RecipeImportError,
    fetch_recipe_page,
    parse_recipe_page,
    validate_url,
)

RECIPE = {
    "@type": "Recipe",
    "name": "<b>Synthetic soup</b>",
    "recipeIngredient": ["2 carrots", "1 tbsp olive oil"],
    "recipeInstructions": [
        {
            "@type": "HowToSection",
            "name": "Cook",
            "itemListElement": [
                {"@type": "HowToStep", "text": "<p>Boil &amp; stir.</p>"}
            ],
        }
    ],
    "recipeYield": ["4 servings"],
    "recipeCategory": "Dinner",
    "recipeCuisine": ["Test cuisine"],
    "keywords": "quick, easy",
    "prepTime": "PT10M",
}


def page(value: object) -> str:
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def test_graph_instructions_categories_and_original_text() -> None:
    html = page(
        {"@context": "https://schema.org", "@graph": [{"@type": "WebSite"}, RECIPE]}
    )
    result = parse_recipe_page(html, "https://example.com/soup", include_tags=True)
    assert result == {
        "title": "Synthetic soup",
        "source": "https://example.com/soup",
        "serving_size": "4 servings",
        "ingredients": "2 carrots\n1 tbsp olive oil",
        "directions": "Cook\nBoil & stir.",
        "notes": "prepTime: PT10M",
        "categories": ["Dinner", "Test cuisine", "quick", "easy"],
    }
    assert parse_recipe_page(html, "https://example.com/soup")["categories"] == [
        "Dinner",
        "Test cuisine",
    ]


def test_ambiguous_page_requires_explicit_index() -> None:
    html = page([RECIPE, {**RECIPE, "name": "Second recipe"}])
    with pytest.raises(RecipeImportError, match="2 recipes"):
        parse_recipe_page(html, "https://example.com")
    assert (
        parse_recipe_page(html, "https://example.com", recipe_index=1)["title"]
        == "Second recipe"
    )
    with pytest.raises(RecipeImportError, match="outside"):
        parse_recipe_page(html, "https://example.com", recipe_index=2)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {**RECIPE, "recipeInstructions": []},
        {**RECIPE, "recipeIngredient": []},
        {**RECIPE, "name": ""},
    ],
)
def test_missing_required_content_is_not_invented(value: dict) -> None:
    with pytest.raises(RecipeImportError):
        parse_recipe_page(page(value), "https://example.com")


def test_nested_data_is_bounded() -> None:
    nested = RECIPE
    for _ in range(35):
        nested = {"child": nested}
    with pytest.raises(RecipeImportError, match="complex"):
        parse_recipe_page(page(nested), "https://example.com")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "https://user:password@example.com/",
        "https://example.com:8443/",
        "https://127.0.0.1/",
        "https://10.0.0.1/",
        "https://169.254.169.254/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[64:ff9b::a00:1]/",
        "https://[2002:7f00:1::]/",
        "https://[2606:4700::1111%25en0]/",
        "https://example.com/\nheader",
        "https://2130706433/",
        "https://127.1/",
        "https://0177.0.0.1/",
        "https://127.000.000.1/",
        "https://127。0。0。1/",
        "https://１２７．０．０．１/",
        "https://169｡254｡169｡254/",
        "https://[fec0::1]/",
        "https://[feff::1]/",
        "https://[::ffff:0:7f00:1]/",
    ],
)
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(RecipeImportError, match="public HTTPS"):
        validate_url(url)


@pytest.mark.asyncio
async def test_exact_dns_answers_must_all_be_public() -> None:
    resolver = PublicResolver()
    resolver.resolver = AsyncMock()
    resolver.resolver.resolve.return_value = [
        {"host": "93.184.216.34"},
        {"host": "192.168.1.1"},
    ]
    with pytest.raises(RecipeImportError, match="non-public"):
        await resolver.resolve("example.com", 443)
    resolver.resolver.resolve.return_value = [{"host": "93.184.216.34"}]
    assert await resolver.resolve("example.com", 443) == [{"host": "93.184.216.34"}]
    await resolver.close()


class Response:
    def __init__(
        self, body: bytes = b"", *, status: int = 200, headers: dict | None = None
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content_length = None
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size: int):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Mock:
    instance = Mock()
    manager = AsyncMock()
    manager.__aenter__.return_value = instance
    factory = Mock(return_value=manager)
    instance.session_factory = factory
    monkeypatch.setattr("recipe_assistant.recipe_import.aiohttp.ClientSession", factory)
    return instance


@pytest.mark.asyncio
async def test_redirect_rechecked_without_contacting_private_host(
    session: Mock,
) -> None:
    session.get.return_value = Response(
        status=302, headers={"Location": "https://127.0.0.1/private"}
    )
    with pytest.raises(RecipeImportError, match="public HTTPS"):
        await fetch_recipe_page("https://example.com")
    session.get.assert_called_once_with("https://example.com", allow_redirects=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(b"x" * (MAX_PAGE_BYTES + 1)),
        Response(headers={"Content-Encoding": "gzip"}),
    ],
    ids=["body-limit", "no-unbounded-decompression"],
)
async def test_fetch_limits(session: Mock, response: Response) -> None:
    session.get.return_value = response
    with pytest.raises(RecipeImportError):
        await fetch_recipe_page("https://example.com")


@pytest.mark.asyncio
async def test_valid_page_redirect_and_isolated_session(
    session: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.get.side_effect = [
        Response(status=302, headers={"Location": "/soup"}),
        Response(b"<html>synthetic</html>"),
    ]
    assert await fetch_recipe_page("https://example.com") == (
        "<html>synthetic</html>",
        "https://example.com/soup",
    )
    settings = session.session_factory.call_args.kwargs
    assert settings["trust_env"] is False
    assert settings["auto_decompress"] is False
    assert settings["headers"]["Accept-Encoding"] == "identity"
    assert "Authorization" not in settings["headers"]
