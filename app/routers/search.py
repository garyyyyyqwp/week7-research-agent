"""Search Router — Web search, content fetching, site search."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.services.web_search import search_web, search_web_mock
from app.services.content_fetcher import fetch_url, validate_public_url
from app.services.site_registry import search_site, list_sites

router = APIRouter(tags=["search"])

# SSRF 校验已下沉到 content_fetcher.validate_public_url（服务层统一守卫），
# 这里保留路由层校验以便尽早返回 422 而不是 200+error
_validate_public_url = validate_public_url


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------

class SearchWebRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="搜索查询")
    num_results: int = Field(default=5, ge=1, le=10, description="返回结果数")
    mock: bool = Field(default=False, description="使用 Mock 数据进行对比测试")


class FetchUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, description="目标网页 URL")
    max_chars: int = Field(default=3000, ge=500, le=10000, description="最大返回字符数")
    strategy: str = Field(default="jina", description="提取策略: jina 或 bs4")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_public_url(v)


class SearchSiteRequest(BaseModel):
    site_id: str = Field(..., min_length=1, description="站点 ID (pubmed/arxiv/who/cdc/github)")
    query: str = Field(..., min_length=1, max_length=500, description="搜索查询")
    num_results: int = Field(default=5, ge=1, le=10, description="返回结果数")

    @field_validator("site_id")
    @classmethod
    def validate_site_id(cls, v: str) -> str:
        valid = {"pubmed", "arxiv", "semantic_scholar", "who", "cdc", "github"}
        if v.lower() not in valid:
            raise ValueError(f"不支持的站点ID: {v}。可用: {', '.join(sorted(valid))}")
        return v.lower()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/web")
async def api_search_web(request: SearchWebRequest):
    """搜索互联网（Tavily API 或 Mock）。"""
    if request.mock:
        results = await search_web_mock(request.query, request.num_results)
        source = "mock"
    else:
        results = await search_web(request.query, request.num_results)
        source = "tavily"

    return {
        "query": request.query,
        "source": source,
        "count": len(results),
        "results": results,
    }


@router.post("/fetch")
async def api_fetch_url(request: FetchUrlRequest):
    """提取网页正文内容（Jina Reader 或 bs4）。"""
    result = await fetch_url(
        url=request.url,
        max_chars=request.max_chars,
        strategy=request.strategy,
    )
    if result.get("error") and result.get("strategy") == "error":
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/site")
async def api_search_site(request: SearchSiteRequest):
    """定向搜索指定站点（PubMed/arXiv/WHO/CDC 等）。"""
    result = await search_site(
        site_id=request.site_id,
        query=request.query,
        num_results=request.num_results,
    )
    if result.get("error") and result.get("strategy") == "none":
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/sites")
async def api_list_sites(category: str | None = None):
    """列出所有已注册的可搜索站点。"""
    sites = list_sites(category=category)
    return {
        "count": len(sites),
        "sites": sites,
    }
