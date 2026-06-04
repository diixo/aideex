import json
from urllib.parse import urlparse
import requests
from pathlib import Path
import aiohttp
from asgiref.sync import async_to_sync
from django.conf import settings


def _backend(path: str) -> str:
    return f"{settings.BACKEND_URL}{path}"


def backend_get(path: str, params: dict = None) -> dict:
    raw, status = async_to_sync(make_get_request)(_backend(path), params=params or {})
    return json.loads(raw)


def backend_post(path: str, data: dict = None) -> dict:
    raw, status = async_to_sync(make_post_request)(_backend(path), data=data or {})
    return json.loads(raw)





async def get_request(session, url, params):
    async with session.get(url, params=params) as response:
        return await response.content.read(), response.status


async def make_get_request(url, params=None):
    if not params:
        params = {}
    async with aiohttp.ClientSession() as session:
        return await get_request(session, url, params=params)


async def post_request(session, url, data):
    async with session.post(url, json=data) as response:
        return await response.content.read(), response.status


async def make_post_request(url, data=None):
    if not data:
        data = {}
    async with aiohttp.ClientSession() as session:
        return await post_request(session, url, data=data)


async def put_request(session, url, data):
    async with session.put(url, data=data) as response:
        return await response.content.read(), response.status


async def make_put_request(url, data=None):
    if not data:
        data = {}
    async with aiohttp.ClientSession() as session:
        return await put_request(session, url, data=data)


async def delete_request(session, url):
    async with session.delete(url) as response:
        return await response.content.read(), response.status


async def make_delete_request(url):
    async with aiohttp.ClientSession() as session:
        return await delete_request(session, url)

