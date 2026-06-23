"""Unit tests for PubMedConnector — XML parsing + normalize + fetch plumbing.

The XML samples mirror the live efetch ``PubmedArticleSet`` shape verified
during the build (structured AbstractText with Label attrs; PMID, Journal,
PubDate, AuthorList, MeshHeadingList, ELocationID doi).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from core.connector import DataSource
from core.document import Document
from servers.pubmed.connector import (
    CONNECTOR_CLASS,
    PubMedConnector,
    parse_pubmed_xml,
)

_SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE">
      <PMID Version="1">37272534</PMID>
      <Article PubModel="Print-Electronic">
        <Journal>
          <Title>The New England journal of medicine</Title>
          <JournalIssue>
            <PubDate>
              <Year>2023</Year>
              <Month>Jul</Month>
              <Day>27</Day>
            </PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Osimertinib in <i>EGFR</i>-Mutated Lung Cancer.</ArticleTitle>
        <ELocationID EIdType="doi" ValidYN="Y">10.1056/NEJMoa2303269</ELocationID>
        <Abstract>
          <AbstractText Label="BACKGROUND">Osimertinib is a third-gen inhibitor.</AbstractText>
          <AbstractText Label="RESULTS">Disease-free survival was longer.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author><LastName>Tsuboi</LastName><ForeName>Masahiro</ForeName></Author>
          <Author><LastName>Herbst</LastName><ForeName>Roy S</ForeName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D016449">Randomized Controlled Trial</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName UI="D002289">Carcinoma, Non-Small-Cell Lung</DescriptorName>
        </MeshHeading>
        <MeshHeading>
          <DescriptorName UI="D004317">Antineoplastic Agents</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
      <KeywordList Owner="NOTNLM">
        <Keyword MajorTopicYN="N">EGFR</Keyword>
      </KeywordList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">37272534</ArticleId>
        <ArticleId IdType="doi">10.1056/NEJMoa2303269</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

_SPARSE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>99</PMID>
      <Article>
        <Journal><Title>J Test</Title></Journal>
        <ArticleTitle>A minimal record.</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


# --- protocol + parsing ----------------------------------------------------


def test_connector_satisfies_datasource_protocol():
    c = PubMedConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "pubmed"
    assert CONNECTOR_CLASS is PubMedConnector


def test_parse_pubmed_xml_extracts_all_fields():
    recs = parse_pubmed_xml(_SAMPLE_XML)
    assert len(recs) == 1
    r = recs[0]
    assert r["pmid"] == "37272534"
    # Inline <i> markup is flattened into the title text.
    assert r["title"] == "Osimertinib in EGFR-Mutated Lung Cancer."
    # Structured abstract sections are labelled + joined.
    assert "BACKGROUND: Osimertinib is a third-gen inhibitor." in r["abstract"]
    assert "RESULTS: Disease-free survival was longer." in r["abstract"]
    assert r["journal"] == "The New England journal of medicine"
    assert r["authors"] == ["Masahiro Tsuboi", "Roy S Herbst"]
    assert r["doi"] == "10.1056/NEJMoa2303269"
    assert "Carcinoma, Non-Small-Cell Lung" in r["mesh_terms"]
    assert r["keywords"] == ["EGFR"]
    assert "Randomized Controlled Trial" in r["publication_types"]
    assert r["pubdate_iso"].startswith("2023-07-27")


def test_parse_pubmed_xml_handles_sparse_record():
    recs = parse_pubmed_xml(_SPARSE_XML)
    assert len(recs) == 1
    r = recs[0]
    assert r["pmid"] == "99"
    assert r["abstract"] == ""
    assert r["authors"] == []
    assert r["doi"] == ""
    assert r["pubdate_iso"] is None


def test_parse_pubmed_xml_bad_xml_returns_empty():
    assert parse_pubmed_xml("<not valid") == []


# --- normalize -------------------------------------------------------------


def test_normalize_maps_record_to_document():
    raw = parse_pubmed_xml(_SAMPLE_XML)[0]
    doc = PubMedConnector().normalize(raw)

    assert isinstance(doc, Document)
    assert doc.source_id == "pubmed"
    assert doc.doc_id == "37272534"
    assert doc.title.startswith("Osimertinib")
    assert doc.url == "https://pubmed.ncbi.nlm.nih.gov/37272534/"
    assert doc.updated_at.year == 2023 and doc.updated_at.month == 7

    # embed_text leads with title + abstract, then controlled vocabulary.
    assert "Osimertinib" in doc.embed_text
    assert "Disease-free survival" in doc.embed_text
    assert "Carcinoma, Non-Small-Cell Lung" in doc.embed_text

    s = doc.structured
    assert s["pmid"] == "37272534"
    assert s["has_abstract"] is True
    assert s["doi"] == "10.1056/NEJMoa2303269"
    assert s["journal"] == "The New England journal of medicine"


def test_normalize_sparse_record_never_empty_embed_text():
    raw = parse_pubmed_xml(_SPARSE_XML)[0]
    doc = PubMedConnector().normalize(raw)
    assert doc.doc_id == "99"
    # embed_text falls back to the title (canonical non-empty contract).
    assert doc.embed_text == "A minimal record."
    assert doc.structured["has_abstract"] is False
    assert doc.updated_at is not None  # falls back to ingest-time


def test_normalize_untitled_record():
    doc = PubMedConnector().normalize({"pmid": "5"})
    assert doc.title == "(untitled article)"
    assert doc.embed_text == "(untitled article)"
    assert doc.url == "https://pubmed.ncbi.nlm.nih.gov/5/"


# --- query building + get_raw ---------------------------------------------


def test_build_term_adds_since_pdat_clause():
    from datetime import UTC, datetime

    c = PubMedConnector(term="lung cancer")
    term = c._build_term(datetime(2026, 1, 15, tzinfo=UTC))
    assert "(lung cancer)" in term
    assert '"2026/01/15"[pdat]' in term


def test_build_term_since_only_when_no_term():
    from datetime import UTC, datetime

    c = PubMedConnector(term="")
    term = c._build_term(datetime(2026, 1, 1, tzinfo=UTC))
    assert "[pdat]" in term
    # No leading empty "() AND".
    assert not term.startswith("()")


async def test_get_raw_rejects_non_numeric_pmid():
    c = PubMedConnector()
    assert await c.get_raw("not-a-pmid") is None
    assert await c.get_raw("") is None


async def test_get_raw_efetches_single_pmid():
    c = PubMedConnector()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = _SAMPLE_XML
    resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=resp)
    # AsyncClient(...) used as an async context manager.
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("servers.pubmed.connector.httpx.AsyncClient", return_value=cm):
        raw = await c.get_raw("37272534")
    assert raw is not None
    assert raw["pmid"] == "37272534"


async def test_fetch_with_no_term_and_no_since_yields_nothing():
    c = PubMedConnector(term="")
    got = [r async for r in c.fetch(None)]
    assert got == []
