#!/usr/bin/env python3
"""
MCP Server for PubMed
Query PubMed/NCBI databases via the E-utilities API.

Tools:
  - search_pubmed        : Search articles by keyword/query
  - get_article          : Get full details of an article by PMID
  - get_full_text        : Retrieve full text from PubMed Central (PMC)
  - get_related_articles : Find articles related to a given PMID
  - search_by_author     : Search all articles by a specific author
"""

import asyncio
import logging
import os
import sys
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# Redirect all logs to stderr — stdout is reserved for JSON-RPC
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NCBI_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Set via environment variable — see .env.example
# Without key: 3 req/s  |  With key: 10 req/s
NCBI_API_KEY: Optional[str] = os.environ.get("NCBI_API_KEY") or None

# Retry settings
MAX_RETRIES = 3
RETRY_DELAY = 1.5  # seconds between retries

mcp = FastMCP(
    "pubmed",
    instructions=(
        "Use this server to search and retrieve scientific articles from PubMed. "
        "You can search by keyword, author, PMID, or retrieve full text when available. "
        "Set the NCBI_API_KEY environment variable for higher rate limits (10 req/s vs 3 req/s)."
    ),
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class PubMedError(Exception):
    """Raised when the NCBI API returns an unexpected response."""


async def _get(endpoint: str, params: dict) -> httpx.Response:
    """
    Send a GET request to an NCBI E-utilities endpoint.
    Retries up to MAX_RETRIES times on transient errors (429, 5xx, timeouts).
    """
    params = dict(params)  # copy so we don't mutate caller's dict
    params.setdefault("tool", "mcp-pubmed")
    params.setdefault("email", "mcp-pubmed@users.noreply.github.com")
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY

    url = f"{NCBI_BASE_URL}/{endpoint}"
    last_error: Exception = RuntimeError("Unknown error")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params)

                # Rate-limited: back off and retry
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", RETRY_DELAY))
                    await asyncio.sleep(retry_after)
                    last_error = PubMedError(
                        f"Rate limited by NCBI (HTTP 429). "
                        f"Consider setting NCBI_API_KEY for 10 req/s."
                    )
                    continue

                # Transient server errors: retry
                if response.status_code >= 500:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    last_error = PubMedError(
                        f"NCBI server error (HTTP {response.status_code}). "
                        f"Attempt {attempt}/{MAX_RETRIES}."
                    )
                    continue

                # Client errors (400, 404, …): do not retry
                if response.status_code >= 400:
                    raise PubMedError(
                        f"NCBI API returned HTTP {response.status_code}. "
                        f"Check your query parameters."
                    )

                # Check for NCBI error messages embedded in the response
                if response.headers.get("content-type", "").startswith("text/xml"):
                    if "<ERROR>" in response.text:
                        root = ET.fromstring(response.text)
                        err_el = root.find(".//ERROR")
                        err_msg = err_el.text if err_el is not None else "Unknown NCBI error"
                        raise PubMedError(f"NCBI API error: {err_msg}")

                return response

        except httpx.TimeoutException:
            last_error = PubMedError(
                f"Request timed out (attempt {attempt}/{MAX_RETRIES}). "
                f"NCBI may be slow — try again later."
            )
            await asyncio.sleep(RETRY_DELAY * attempt)

        except httpx.ConnectError:
            last_error = PubMedError(
                "Could not connect to NCBI. Check your internet connection."
            )
            await asyncio.sleep(RETRY_DELAY * attempt)

        except PubMedError:
            raise  # Already formatted — propagate immediately

    raise last_error


def _require_xml(response: httpx.Response, context: str) -> ET.Element:
    """Parse XML response; raise PubMedError with context on failure."""
    try:
        return ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise PubMedError(
            f"Could not parse NCBI XML response ({context}): {exc}"
        ) from exc


def _parse_article(article_xml: ET.Element) -> dict:
    """Parse a <PubmedArticle> XML element into a plain dict."""
    art: dict = {}

    # PMID
    el = article_xml.find(".//PMID")
    if el is not None:
        art["pmid"] = el.text

    # Title
    el = article_xml.find(".//ArticleTitle")
    if el is not None:
        art["title"] = "".join(el.itertext()).strip()

    # Abstract (structured or plain)
    abstract_texts = article_xml.findall(".//AbstractText")
    if abstract_texts:
        parts = []
        for at in abstract_texts:
            label = at.get("Label", "")
            text = "".join(at.itertext()).strip()
            parts.append(f"{label}: {text}" if label else text)
        art["abstract"] = "\n".join(parts)

    # Authors
    authors: list[str] = []
    for author in article_xml.findall(".//Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            authors.append(collective)
        else:
            last = author.findtext("LastName", "")
            initials = author.findtext("Initials", "")
            name = f"{last} {initials}".strip()
            if name:
                authors.append(name)
    art["authors"] = authors

    # Journal
    el = article_xml.find(".//Journal/Title")
    if el is not None:
        art["journal"] = el.text

    # Publication date
    pub_date_el = article_xml.find(".//PubDate")
    if pub_date_el is not None:
        parts = [
            pub_date_el.findtext("Year", ""),
            pub_date_el.findtext("Month", ""),
            pub_date_el.findtext("Day", ""),
        ]
        art["pub_date"] = " ".join(p for p in parts if p).strip()

    # IDs: DOI and PMC
    for id_el in article_xml.findall(".//ArticleId"):
        id_type = id_el.get("IdType", "")
        if id_type == "doi" and id_el.text:
            art["doi"] = id_el.text.strip()
        elif id_type == "pmc" and id_el.text:
            art["pmc_id"] = id_el.text.strip()

    # Keywords
    kws = [kw.text.strip() for kw in article_xml.findall(".//Keyword") if kw.text]
    if kws:
        art["keywords"] = kws

    # MeSH terms
    mesh = [
        d.text.strip()
        for d in article_xml.findall(".//MeshHeading/DescriptorName")
        if d.text
    ]
    if mesh:
        art["mesh_terms"] = mesh

    # Publication types
    ptypes = [
        pt.text.strip()
        for pt in article_xml.findall(".//PublicationType")
        if pt.text
    ]
    if ptypes:
        art["publication_types"] = ptypes

    return art


def _format_brief(art: dict, index: int) -> str:
    """Format one article as a short multi-line block."""
    lines = [f"--- Article {index} ---"]
    lines.append(f"PMID    : {art.get('pmid', 'N/A')}")
    lines.append(f"Title   : {art.get('title', 'N/A')}")

    authors = art.get("authors", [])
    if authors:
        label = ", ".join(authors[:3])
        if len(authors) > 3:
            label += f" et al. ({len(authors)} authors)"
        lines.append(f"Authors : {label}")

    lines.append(f"Journal : {art.get('journal', 'N/A')}")
    lines.append(f"Date    : {art.get('pub_date', 'N/A')}")

    if "doi" in art:
        lines.append(f"DOI     : {art['doi']}")
    if "pmc_id" in art:
        lines.append(f"PMC     : {art['pmc_id']}")

    abstract = art.get("abstract", "")
    if abstract:
        snippet = abstract[:500] + "…" if len(abstract) > 500 else abstract
        lines.append(f"Abstract: {snippet}")

    lines.append(f"URL     : https://pubmed.ncbi.nlm.nih.gov/{art.get('pmid', '')}/")
    return "\n".join(lines)


def _err(msg: str) -> str:
    """Return a user-facing error string."""
    return f"[Error] {msg}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_pubmed(
    query: str,
    max_results: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    article_type: Optional[str] = None,
    sort: str = "relevance",
) -> str:
    """Search PubMed for articles matching a query.

    Args:
        query: Search query. Supports full PubMed syntax:
               AND / OR / NOT, field tags like [tiab], [MeSH], [au], etc.
               Examples:
                 "covid-19 vaccine efficacy"
                 "myocardial infarction[MeSH] AND aspirin[tiab]"
        max_results: Number of articles to return (1-100, default 10).
        year_from: Restrict results to articles published from this year.
        year_to:   Restrict results to articles published up to this year.
        article_type: Filter by publication type, e.g. "Review",
                      "Clinical Trial", "Meta-Analysis",
                      "Randomized Controlled Trial".
        sort: "relevance" (default) or "date" (most recent first).

    Returns:
        A formatted list of matching articles with PMID, title, authors,
        journal, date, and a short abstract snippet.
        Returns an error message if the query fails or yields no results.
    """
    if not query or not query.strip():
        return _err("Query must not be empty.")

    max_results = max(1, min(max_results, 100))

    # Build full query with optional filters
    full_query = query.strip()
    if year_from and year_to:
        if year_from > year_to:
            return _err(f"year_from ({year_from}) must be ≤ year_to ({year_to}).")
        full_query += f" AND {year_from}:{year_to}[pdat]"
    elif year_from:
        full_query += f" AND {year_from}:3000[pdat]"
    elif year_to:
        full_query += f" AND 1900:{year_to}[pdat]"

    if article_type:
        full_query += f' AND "{article_type.strip()}"[pt]'

    sort_param = "pub_date" if sort == "date" else "relevance"

    try:
        # Step 1: esearch → PMIDs
        search_resp = await _get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": full_query,
                "retmax": max_results,
                "retmode": "json",
                "sort": sort_param,
            },
        )
        esearch = search_resp.json().get("esearchresult", {})
        id_list: list[str] = esearch.get("idlist", [])
        total: str = esearch.get("count", "0")

        # Warn if query was corrected/translated
        query_translation = esearch.get("querytranslation", "")

        if not id_list:
            return (
                f"No articles found for query: {query!r}\n"
                f"(Full query sent: {full_query})"
            )

        # Step 2: efetch → article XML
        fetch_resp = await _get(
            "efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(id_list),
                "retmode": "xml",
                "rettype": "abstract",
            },
        )
        root = _require_xml(fetch_resp, "efetch articles")
        articles = [_parse_article(a) for a in root.findall(".//PubmedArticle")]

        if not articles:
            return _err("Received empty article list from NCBI.")

        header_parts = [f"Found {total} total result(s). Showing {len(articles)}."]
        if query_translation:
            header_parts.append(f"Query interpreted as: {query_translation}")
        header = "\n".join(header_parts) + "\n"

        blocks = [_format_brief(a, i) for i, a in enumerate(articles, 1)]
        return header + "\n\n".join(blocks)

    except PubMedError as exc:
        return _err(str(exc))


@mcp.tool()
async def get_article(pmid: str) -> str:
    """Get complete details of a PubMed article by its PMID.

    Args:
        pmid: The PubMed ID (numeric string), e.g. "33982811".

    Returns:
        Full article metadata: title, all authors, journal, date, DOI,
        PMC link, publication types, full abstract, keywords, MeSH terms.
        Returns an error message if the PMID is invalid or not found.
    """
    pmid = pmid.strip()
    if not pmid.isdigit():
        return _err(f"Invalid PMID: {pmid!r}. A PMID must be a numeric string.")

    try:
        fetch_resp = await _get(
            "efetch.fcgi",
            {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"},
        )
        root = _require_xml(fetch_resp, f"efetch PMID {pmid}")
        article_xml = root.find(".//PubmedArticle")

        if article_xml is None:
            return _err(f"No article found for PMID {pmid}. It may not exist or may have been retracted.")

        art = _parse_article(article_xml)

        lines = [f"=== PubMed Article — PMID {pmid} ==="]
        lines.append(f"Title    : {art.get('title', 'N/A')}")

        authors = art.get("authors", [])
        if authors:
            lines.append(f"Authors  : {', '.join(authors)}")

        lines.append(f"Journal  : {art.get('journal', 'N/A')}")
        lines.append(f"Published: {art.get('pub_date', 'N/A')}")

        if "doi" in art:
            lines.append(f"DOI      : {art['doi']}")
            lines.append(f"DOI URL  : https://doi.org/{art['doi']}")

        if "pmc_id" in art:
            pmc_id = art["pmc_id"]
            lines.append(f"PMC ID   : {pmc_id}")
            lines.append(f"PMC URL  : https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/")

        lines.append(f"PubMed   : https://pubmed.ncbi.nlm.nih.gov/{pmid}/")

        if "publication_types" in art:
            lines.append(f"Type(s)  : {', '.join(art['publication_types'])}")

        if "abstract" in art:
            lines.append(f"\nAbstract:\n{art['abstract']}")
        else:
            lines.append("\nAbstract: Not available.")

        if "keywords" in art:
            lines.append(f"\nKeywords : {', '.join(art['keywords'])}")

        if "mesh_terms" in art:
            lines.append(f"\nMeSH     : {', '.join(art['mesh_terms'])}")

        return "\n".join(lines)

    except PubMedError as exc:
        return _err(str(exc))


@mcp.tool()
async def get_full_text(pmid: str) -> str:
    """Retrieve the full text of an article from PubMed Central (PMC) if available.

    Only open-access articles archived in PMC have a full text.
    Subscription-only articles will return a link to PubMed instead.

    Args:
        pmid: The PubMed ID of the article.

    Returns:
        The full text (title, abstract, and body sections) when the article
        is open-access in PMC, or a helpful message with links otherwise.
    """
    pmid = pmid.strip()
    if not pmid.isdigit():
        return _err(f"Invalid PMID: {pmid!r}. A PMID must be a numeric string.")

    try:
        # 1) Retrieve the PubMed record to find the PMC ID
        fetch_resp = await _get(
            "efetch.fcgi",
            {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"},
        )
        root = _require_xml(fetch_resp, f"efetch PMID {pmid}")
        article_xml = root.find(".//PubmedArticle")

        if article_xml is None:
            return _err(f"No article found for PMID {pmid}.")

        pmc_id: Optional[str] = None
        for id_el in article_xml.findall(".//ArticleId"):
            if id_el.get("IdType") == "pmc" and id_el.text:
                pmc_id = id_el.text.strip()
                break

        if not pmc_id:
            title_el = article_xml.find(".//ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            msg = [f"Full text not available in PubMed Central for PMID {pmid}."]
            if title:
                msg.append(f"Title: {title}")
            msg.append(
                "The article may be subscription-only or not yet indexed in PMC.\n"
                f"PubMed page: https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            )
            return "\n".join(msg)

        # 2) Fetch full-text XML from PMC
        numeric_id = pmc_id.replace("PMC", "")
        pmc_resp = await _get(
            "efetch.fcgi",
            {"db": "pmc", "id": numeric_id, "retmode": "xml", "rettype": "full"},
        )

        try:
            pmc_root = ET.fromstring(pmc_resp.text)
        except ET.ParseError as exc:
            return (
                f"Full text is available in PMC but could not be parsed.\n"
                f"Reason: {exc}\n"
                f"PMC URL: https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/"
            )

        sections: list[str] = []

        title_el = pmc_root.find(".//article-title")
        if title_el is not None:
            sections.append(f"TITLE\n{''.join(title_el.itertext()).strip()}\n")

        abstract_el = pmc_root.find(".//abstract")
        if abstract_el is not None:
            sections.append("ABSTRACT\n" + "".join(abstract_el.itertext()).strip() + "\n")

        body = pmc_root.find(".//body")
        if body is not None:
            for sec in body.findall(".//sec"):
                title_el = sec.find("title")
                if title_el is not None:
                    sections.append(f"\n{''.join(title_el.itertext()).upper()}")
                for p in sec.findall("p"):
                    para = "".join(p.itertext()).strip()
                    if para:
                        sections.append(para)

        if not sections:
            return (
                f"Full text is available in PMC but the content could not be extracted.\n"
                f"PMC URL: https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/"
            )

        header = f"=== Full Text — PMID {pmid} | {pmc_id} ===\n"
        return header + "\n".join(sections)

    except PubMedError as exc:
        return _err(str(exc))


@mcp.tool()
async def get_related_articles(pmid: str, max_results: int = 10) -> str:
    """Find PubMed articles related to a given article.

    Uses NCBI's "similar articles" algorithm (co-citation and text similarity).

    Args:
        pmid: The PubMed ID of the reference article.
        max_results: Number of related articles to return (1-50, default 10).

    Returns:
        A ranked list of related articles with brief metadata.
        Returns an error message if the PMID is invalid or not found.
    """
    pmid = pmid.strip()
    if not pmid.isdigit():
        return _err(f"Invalid PMID: {pmid!r}. A PMID must be a numeric string.")

    max_results = max(1, min(max_results, 50))

    try:
        link_resp = await _get(
            "elink.fcgi",
            {
                "dbfrom": "pubmed",
                "db": "pubmed",
                "id": pmid,
                "cmd": "neighbor_score",
                "retmode": "json",
            },
        )

        link_data = link_resp.json()
        related_pmids: list[str] = []

        try:
            for linkset in link_data.get("linksets", []):
                for lsdb in linkset.get("linksetdbs", []):
                    if lsdb.get("linkname") == "pubmed_pubmed":
                        related_pmids = [
                            str(lid)
                            for lid in lsdb.get("links", [])
                            if str(lid) != pmid
                        ][:max_results]
                        break
        except (KeyError, TypeError) as exc:
            return _err(f"Could not parse related articles response: {exc}")

        if not related_pmids:
            return (
                f"No related articles found for PMID {pmid}.\n"
                f"The article may be too recent or not well-cited yet."
            )

        fetch_resp = await _get(
            "efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(related_pmids),
                "retmode": "xml",
                "rettype": "abstract",
            },
        )
        root = _require_xml(fetch_resp, "efetch related articles")
        articles = [_parse_article(a) for a in root.findall(".//PubmedArticle")]

        if not articles:
            return _err("Received empty article list from NCBI.")

        header = f"Related articles for PMID {pmid} — {len(articles)} result(s):\n"
        blocks = [_format_brief(a, i) for i, a in enumerate(articles, 1)]
        return header + "\n\n".join(blocks)

    except PubMedError as exc:
        return _err(str(exc))


@mcp.tool()
async def search_by_author(
    author: str,
    max_results: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> str:
    """Search PubMed for all articles by a specific author.

    Args:
        author: Author name in PubMed format. Examples:
                "Smith JA"   (last name + initials — most precise)
                "Smith J"    (last name + first initial)
                "John Smith" (full name, less reliable)
        max_results: Number of results to return (1-100, default 10).
        year_from: Restrict to articles published from this year.
        year_to:   Restrict to articles published up to this year.

    Returns:
        A list of articles by the author, sorted by most recent first.
        Returns an error message if the author name is empty or the query fails.
    """
    if not author or not author.strip():
        return _err("Author name must not be empty.")

    return await search_pubmed(
        query=f"{author.strip()}[author]",
        max_results=max_results,
        year_from=year_from,
        year_to=year_to,
        sort="date",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
