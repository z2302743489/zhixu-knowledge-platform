"""Bounded native loaders and Document Mind cloud layout parsing.

No OCR/model weights are downloaded. Cloud calls use Alibaba's official SDK.
Source locations survive normalization into LangChain Documents.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Callable

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_ROWS = 20_000
MAX_CELLS = 200_000
MAX_TEXT_CHARS = 8_000_000
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'md', 'png', 'jpg', 'jpeg', 'webp', 'xlsx', 'xls', 'csv'}
MIME_TYPES = {'pdf': 'application/pdf', 'doc': 'application/msword', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'md': 'text/plain', 'csv': 'text/plain', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xls': 'application/vnd.ms-excel'}


class ParseError(ValueError):
    pass


def validate_file(path: Path, filename: str) -> str:
    kind = Path(filename).suffix.lower().lstrip('.')
    if kind not in ALLOWED_EXTENSIONS:
        raise ParseError('不支持的文件格式')
    size = path.stat().st_size
    if size == 0 or size > MAX_FILE_BYTES:
        raise ParseError('文件为空或超过 50 MB')
    with path.open('rb') as stream:
        head = stream.read(4096)
    magic = {'pdf': b'%PDF-', 'doc': b'\xd0\xcf\x11\xe0', 'xls': b'\xd0\xcf\x11\xe0', 'png': b'\x89PNG\r\n\x1a\n', 'jpg': b'\xff\xd8\xff', 'jpeg': b'\xff\xd8\xff'}
    if kind in magic and not head.startswith(magic[kind]):
        raise ParseError('文件内容与扩展名不匹配')
    if kind == 'webp' and not (head.startswith(b'RIFF') and head[8:12] == b'WEBP'):
        raise ParseError('不是有效的 WebP 文件')
    if kind in {'docx', 'xlsx'}:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10_000 or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                    raise ParseError('Office 文件解压体积或项目数过大')
                expected = 'word/document.xml' if kind == 'docx' else 'xl/workbook.xml'
                if expected not in archive.namelist():
                    raise ParseError('Office 文件结构不符合扩展名')
        except zipfile.BadZipFile as exc:
            raise ParseError('Office 文件已损坏') from exc
    if kind in {'md', 'csv'} and b'\x00' in head:
        raise ParseError('文本文件包含二进制内容')
    return kind


def _decode(path: Path) -> str:
    data = path.read_bytes()
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ParseError('文本编码不受支持，请保存为 UTF-8')


def _doc(text: str, location: str, *, table: bool = False) -> Document:
    return Document(page_content=text, metadata={'location': location, 'table': table})


def _table_documents(rows, sheet: str, *, first_row: int = 1) -> list[Document]:
    """Keep row boundaries and repeat headers when grouping large tables."""
    rows = list(rows)
    if not rows:
        return []
    if len(rows) > MAX_ROWS or sum(len(row) for row in rows) > MAX_CELLS:
        raise ParseError('表格超过行数/单元格上限，请拆分后上传')
    def clean(row):
        values = [str(c if c is not None else '').replace('|', '\\|').replace('\n', ' ') for c in row]
        if any(len(value) > 20_000 for value in values):
            raise ParseError('表格单元格内容超过 20000 字符，请拆分后上传')
        return values
    header = clean(rows[0])
    width = max(len(r) for r in rows)
    header += [''] * (width - len(header))
    heading = '| ' + ' | '.join(header) + ' |\n| ' + ' | '.join(['---'] * width) + ' |\n'
    docs = []
    group, group_start = [], first_row + 1
    for idx, raw in enumerate(rows[1:], start=first_row + 1):
        cells = clean(raw) + [''] * (width - len(raw))
        line = '| ' + ' | '.join(cells) + ' |'
        if group and (sum(len(s) for s in group) + len(line) > 3000 or len(group) >= 30):
            docs.append(_doc(heading + '\n'.join(group), f'{sheet} / 表头第 {first_row} 行；第 {group_start}–{idx - 1} 行', table=True))
            group, group_start = [], idx
        group.append(line)
    if group:
        docs.append(_doc(heading + '\n'.join(group), f'{sheet} / 表头第 {first_row} 行；第 {group_start}–{first_row + len(rows) - 1} 行', table=True))
    elif len(rows) == 1:
        docs.append(_doc(heading, f'{sheet} / 第 {first_row} 行', table=True))
    return docs


def _cloud_client(config: dict):
    from alibabacloud_docmind_api20220711.client import Client
    from alibabacloud_tea_openapi.models import Config
    if not config.get('accessKeyId') or not config.get('accessKeySecret'):
        raise ParseError('请先配置并开通阿里云 Document Mind 云端解析服务')
    region = config.get('region') or 'cn-hangzhou'
    # Document Mind's currently documented public endpoint is Hangzhou.
    if region != 'cn-hangzhou':
        raise ParseError('Document Mind 当前适配 cn-hangzhou 地域，请核对已开通服务')
    return Client(Config(access_key_id=config['accessKeyId'], access_key_secret=config['accessKeySecret'], region_id=region, endpoint='docmind-api.cn-hangzhou.aliyuncs.com', connect_timeout=15000, read_timeout=60000))


def _as_map(data):
    if hasattr(data, 'to_map'):
        return data.to_map()
    if isinstance(data, str):
        return json.loads(data)
    return data or {}


def cloud_parse(path: Path, name: str, config: dict, remote_jobs: dict | None = None, save_remote: Callable[[dict], None] | None = None) -> list[Document]:
    from alibabacloud_docmind_api20220711 import models
    from alibabacloud_tea_util.models import RuntimeOptions
    remote_jobs = remote_jobs if remote_jobs is not None else {}
    client = _cloud_client(config)
    remote_id = remote_jobs.get('id')
    if not remote_id:
        runtime = RuntimeOptions(autoretry=False)
        with path.open('rb') as stream:
            req = models.SubmitDocParserJobAdvanceRequest(file_url_object=stream, file_name=name, llm_enhancement=True)
            result = client.submit_doc_parser_job_advance(req, runtime)
        data = _as_map(result.body.data)
        remote_id = data.get('Id') or data.get('id')
        if not remote_id:
            raise ParseError('Document Mind 提交未返回任务 ID')
        remote_jobs['id'] = remote_id
        if save_remote:
            save_remote(dict(remote_jobs))
    for _ in range(360):
        status = _as_map(client.query_doc_parser_status(models.QueryDocParserStatusRequest(id=remote_id)).body.data)
        phase = str(status.get('Status', status.get('status', ''))).lower()
        if save_remote:
            save_remote({**remote_jobs, 'status': phase, 'parsedBlocks': status.get('NumberOfSuccessfulParsing'), 'estimatedPages': status.get('PageCountEstimate'), 'parsedCharacters': status.get('Tokens')})
        if phase == 'success':
            break
        if phase in {'fail', 'failed', 'error'}:
            raise ParseError('Document Mind 解析失败，请检查文件与云端任务详情')
        time.sleep(5)
    else:
        raise ParseError('Document Mind 解析超时；任务 ID 已保留，重试会继续查询')
    docs, offset = [], 0
    while offset < 100_000:
        raw = _as_map(client.get_doc_parser_result(models.GetDocParserResultRequest(id=remote_id, layout_num=offset, layout_step_size=100)).body.data)
        layouts = raw.get('layouts') or raw.get('Layouts') or []
        if not layouts:
            break
        for item in layouts:
            text = item.get('markdownContent') or item.get('text') or item.get('llmResult') or '\n'.join(str(b.get('text', '')) for b in item.get('blocks', []))
            if not text and item.get('cells'):
                cells = item['cells']
                by_row = {}
                for cell in cells:
                    by_row.setdefault(cell.get('ysc', 0), []).append(cell)
                text = '\n'.join(' | '.join(''.join(str(l.get('text', '')) for l in c.get('layouts', [])) for c in sorted(cs, key=lambda c: c.get('xsc', 0))) for _, cs in sorted(by_row.items()))
            if text.strip():
                page = item.get('pageNum')
                location = f'第 {page} 页 / 版面块 {item.get("index", offset + len(docs))}' if page is not None else f'云端版面块 {item.get("index", offset + len(docs))}'
                if item.get('llmResult') and not item.get('text'):
                    location += ' / 云端模型生成描述'
                docs.append(_doc(text, location, table=item.get('type') == 'table'))
        offset += len(layouts)
        if len(layouts) < 100:
            break
    if not docs:
        raise ParseError('云端未提取到可检索文字或表格')
    if sum(len(d.page_content) for d in docs) > MAX_TEXT_CHARS:
        raise ParseError('云端提取文字超过上限，请拆分文件')
    return docs


def parse_file(path: Path, name: str, *, cloud_parser: bool = True, ocr_config: dict | None = None, remote_jobs: dict | None = None, save_remote=None) -> list[Document]:
    kind = validate_file(path, name)
    if kind in {'doc', 'png', 'jpg', 'jpeg', 'webp'} or (kind == 'pdf' and cloud_parser):
        if not cloud_parser and kind != 'doc':
            raise ParseError('图片识别需要启用云端增强解析')
        if kind == 'webp':
            from PIL import Image
            converted = path.with_suffix('.ocr.png')
            try:
                with Image.open(path) as image:
                    image.convert('RGB').save(converted, 'PNG')
                return cloud_parse(converted, Path(name).stem + '.png', ocr_config or {}, remote_jobs, save_remote)
            finally:
                converted.unlink(missing_ok=True)
        return cloud_parse(path, name, ocr_config or {}, remote_jobs, save_remote)
    if kind == 'md':
        content = _decode(path)
        if re.search(r'!\[[^\]]*\]\([^)]+\)', content):
            raise ParseError('Markdown 包含图片资源。请将图片单独导入以完成云端 OCR，再导入去除图片引用的文字文档')
        docs, buffer, start, heading = [], [], 1, ''
        for line_no, line in enumerate(content.splitlines(), 1):
            if line.startswith('#') and buffer:
                docs.append(_doc('\n'.join(buffer), f'{heading or "正文"} / 第 {start}–{line_no - 1} 行'))
                buffer, start = [], line_no
            if line.startswith('#'):
                heading = line.lstrip('#').strip()
            buffer.append(line)
        if buffer:
            docs.append(_doc('\n'.join(buffer), f'{heading or "正文"} / 第 {start}–{len(content.splitlines())} 行'))
    elif kind == 'pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ParseError('PDF 已加密，请先解密后上传')
        if len(reader.pages) > 2000:
            raise ParseError('本机 PDF 解析上限为 2000 页，请拆分后上传')
        docs = []
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if not text.strip() or list(page.images):
                raise ParseError('PDF 包含图片或扫描页，请启用云端增强解析以避免遗漏内容')
            docs.append(_doc(text, f'第 {i} 页 / 原生文本（表格版面请使用云端增强解析）'))
    elif kind == 'docx':
        from docx import Document as WordDocument
        with zipfile.ZipFile(path) as archive:
            has_images = any(n.startswith('word/media/') for n in archive.namelist())
        if has_images:
            if not cloud_parser:
                raise ParseError('Word 包含内嵌图片，请启用云端增强解析')
            return cloud_parse(path, name, ocr_config or {}, remote_jobs, save_remote)
        word = WordDocument(path)
        docs, heading = [], '正文'
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        for idx, child in enumerate(word.element.body, 1):
            if child.tag == qn('w:p'):
                para = Paragraph(child, word)
                if para.style and para.style.name.startswith('Heading'):
                    heading = para.text.strip()
                if para.text.strip():
                    docs.append(_doc(para.text, f'{heading} / 段落 {idx}'))
            elif child.tag == qn('w:tbl'):
                table = Table(child, word)
                docs += _table_documents([[cell.text for cell in row.cells] for row in table.rows], f'{heading} / 表格 {idx}')
    elif kind == 'xlsx':
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        docs = []
        try:
            if len(workbook.worksheets) > 100:
                raise ParseError('工作表超过 100 个，请拆分后上传')
            for sheet in workbook.worksheets:
                if (sheet.max_row or 0) > MAX_ROWS or (sheet.max_row or 0) * (sheet.max_column or 0) > MAX_CELLS:
                    raise ParseError('表格超过行数/单元格上限')
                rows = [list(row) for row in sheet.iter_rows(values_only=True)]
                docs += _table_documents(rows, f'{sheet.title}（公式使用文件内缓存值；未缓存的公式为空）')
        finally:
            workbook.close()
    elif kind == 'xls':
        import xlrd
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        docs = []
        try:
            if workbook.nsheets > 100:
                raise ParseError('工作表超过 100 个')
            for sheet in workbook.sheets():
                if sheet.nrows > MAX_ROWS or sheet.nrows * sheet.ncols > MAX_CELLS:
                    raise ParseError('表格超过行数/单元格上限')
                docs += _table_documents([sheet.row_values(i) for i in range(sheet.nrows)], sheet.name)
        finally:
            workbook.release_resources()
    elif kind == 'csv':
        content = _decode(path)
        try:
            dialect = csv.Sniffer().sniff(content[:8192], delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel
        rows = []
        for row in csv.reader(io.StringIO(content), dialect):
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ParseError('CSV 超过行数上限')
        docs = _table_documents(rows, 'CSV')
    else:
        raise ParseError('当前解析器不支持该格式')
    if not docs or not any(d.page_content.strip() for d in docs):
        raise ParseError('文件未提取到可检索内容')
    if sum(len(d.page_content) for d in docs) > MAX_TEXT_CHARS:
        raise ParseError('提取文字超过上限，请拆分文件')
    return docs


def split_documents(documents: list[Document], chunk_size: int = 800, overlap: int = 100) -> list[Document]:
    if not 200 <= chunk_size <= 4000 or not 0 <= overlap < chunk_size:
        raise ParseError('分块长度需为 200–4000，重叠需小于分块长度')
    # Character count is explicit in the UI; do not pretend this is model-token counting.
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap, separators=['\n\n', '\n', '。', '；', ' ', ''], add_start_index=True)
    chunks = []
    for document in documents:
        if document.metadata.get('table'):
            # Table row groups already repeat headers; preserve cells/rows intact.
            chunks.append(document)
        else:
            chunks.extend(splitter.split_documents([document]))
    if len(chunks) > 30_000:
        raise ParseError('分块超过 30000，请拆分原文件')
    return chunks
