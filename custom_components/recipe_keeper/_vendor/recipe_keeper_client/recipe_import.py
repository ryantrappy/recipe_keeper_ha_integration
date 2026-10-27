"""Import public recipe pages without executing code or fetching linked resources."""

import asyncio
import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from .models import NewRecipe

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class RecipeImportError(ValueError):
    """The page cannot be safely fetched or contains no usable recipe."""


def public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.scope_id
        or ip.is_site_local
        or any(
            ip in ipaddress.ip_network(network)
            for network in ("64:ff9b::/96", "2002::/16", "2001::/32")
        )
    ):
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_url(url: str) -> str:
    try:
        parts = URL(url)
        if (
            parts.scheme != "https"
            or not parts.raw_host
            or parts.user is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or any(ord(c) < 33 for c in url)
        ):
            raise ValueError
        host = parts.raw_host
        if "%" in host:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError from None
        else:
            if not public_address(host):
                raise ValueError
        return str(parts.with_fragment(None))
    except ValueError:
        raise RecipeImportError(
            "Use a public HTTPS recipe URL on port 443 without credentials"
        ) from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the exact DNS answers used by the HTTP connector."""

    def __init__(self) -> None:
        self.resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list:
        answers = await self.resolver.resolve(host, port, family)
        if not answers or not all(public_address(answer["host"]) for answer in answers):
            raise RecipeImportError("Recipe URL resolves to a non-public address")
        return answers

    async def close(self) -> None:
        await self.resolver.close()


async def fetch_recipe_page(url: str) -> tuple[str, str]:
    """Use an isolated session: no Recipe Keeper auth, cookies, or environment proxy."""
    url = validate_url(url)
    resolver = PublicResolver()
    try:
        async with (
            asyncio.timeout(30),
            aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=resolver, use_dns_cache=False, force_close=True
                ),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": "RecipeKeeperHA/0.3 recipe import",
                    "Accept-Encoding": "identity",
                },
            ) as session,
        ):
            for hop in range(MAX_REDIRECTS + 1):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if hop == MAX_REDIRECTS or not location:
                            raise RecipeImportError("Recipe URL has too many redirects")
                        url = validate_url(urljoin(url, location))
                        continue
                    if response.status != 200:
                        raise RecipeImportError("Recipe website did not return a page")
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise RecipeImportError(
                            "Recipe website must allow uncompressed HTML"
                        )
                    if (
                        response.content_length
                        and response.content_length > MAX_PAGE_BYTES
                    ):
                        raise RecipeImportError("Recipe page is too large")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_PAGE_BYTES:
                            raise RecipeImportError("Recipe page is too large")
                    return body.decode(
                        response.charset or "utf-8", errors="replace"
                    ), url
    except (aiohttp.ClientError, TimeoutError, OSError, LookupError):
        raise RecipeImportError("Could not download the public recipe page") from None
    finally:
        await resolver.close()
    raise RecipeImportError("Recipe page is unavailable")


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if (
            tag == "script"
            and (dict(attrs).get("type") or "").lower().split(";")[0].strip()
            == "application/ld+json"
        ):
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.documents.append("".join(self.current))
            self.current = None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("br", "p", "li", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "li", "div"):
            self.parts.append("\n")


def _text(value: object) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    parser = _Text()
    parser.feed(str(value))
    return "\n".join(
        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
    )


def _instructions(value: object, depth: int = 0) -> list[str]:
    if depth > 20:
        raise RecipeImportError("Recipe instructions are nested too deeply")
    if isinstance(value, list):
        return [line for item in value for line in _instructions(item, depth + 1)]
    if isinstance(value, dict):
        if "itemListElement" in value:
            heading = _text(value.get("name"))
            return ([heading] if heading else []) + _instructions(
                value["itemListElement"], depth + 1
            )
        return _instructions(value.get("text") or value.get("name"), depth + 1)
    text = _text(value)
    return [text] if text else []


def parse_recipe_page(
    html: str, url: str, *, include_tags: bool = False, recipe_index: int | None = None
) -> dict:
    page = _Page()
    page.feed(html)
    candidates: list[dict] = []
    visited = 0
    for document in page.documents:
        try:
            stack = [(json.loads(document), 0)]
        except (ValueError, RecursionError):
            continue
        while stack:
            value, depth = stack.pop()
            visited += 1
            if visited > 10000 or depth > 30:
                raise RecipeImportError("Recipe structured data is too complex")
            if isinstance(value, dict):
                types = value.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(
                    isinstance(t, str) and re.split(r"[/#]", t)[-1] == "Recipe"
                    for t in types
                ):
                    if value not in candidates:
                        candidates.append(value)
                stack.extend(
                    (item, depth + 1) for item in reversed(list(value.values()))
                )
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in reversed(value))
    if not candidates:
        raise RecipeImportError("No schema.org Recipe JSON-LD found on this page")
    if recipe_index is None and len(candidates) > 1:
        raise RecipeImportError(
            f"Page contains {len(candidates)} recipes; "
            "choose recipe_index starting at 0"
        )
    index = recipe_index if recipe_index is not None else 0
    if index < 0 or index >= len(candidates):
        raise RecipeImportError("recipe_index is outside the page's recipe list")
    recipe = candidates[index]
    ingredients = recipe.get("recipeIngredient", [])
    ingredients = [ingredients] if isinstance(ingredients, str) else ingredients
    if not isinstance(ingredients, list):
        raise RecipeImportError("Recipe ingredients have an unsupported format")
    title = _text(recipe.get("name"))
    ingredient_text = "\n".join(filter(None, (_text(item) for item in ingredients)))
    directions = "\n".join(_instructions(recipe.get("recipeInstructions")))
    if not title or not ingredient_text or not directions:
        raise RecipeImportError(
            "Recipe must contain a name, ingredients, and instructions"
        )
    categories = []
    for key in (
        "recipeCategory",
        "recipeCuisine",
        *(("keywords",) if include_tags else ()),
    ):
        values = recipe.get(key, [])
        values = values.split(",") if isinstance(values, str) else values
        if isinstance(values, list):
            categories.extend(filter(None, (_text(item) for item in values)))
    notes = [_text(recipe.get("description"))]
    for key in ("prepTime", "cookTime", "totalTime"):
        if value := _text(recipe.get(key)):
            notes.append(f"{key}: {value}")
    serving = recipe.get("recipeYield", "")
    if isinstance(serving, list):
        serving = ", ".join(filter(None, (_text(item) for item in serving)))
    return NewRecipe(
        title=title,
        source=url,
        serving_size=_text(serving),
        ingredients=ingredient_text,
        directions=directions,
        notes="\n".join(filter(None, notes)),
        categories=list(dict.fromkeys(categories)),
    ).model_dump(mode="json", exclude_unset=True)
