"""具体解析器实现：支持 pdf, docx, doc, csv, xls, xlsx, txt, md
依赖：
pip install
"""
import os
import io
import subprocess
# import typing
import tempfile
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

# PDF - 使用 PyMuPDF (fitz)
try:
    import fitz  # PyMuPDF
except ImportError:
    import pymupdf as fitz  # 新版本推荐；旧代码常用 import fitz
except Exception:
    fitz = None

# DOCX
try:
    import docx
    from docx import Document
except Exception:
    docx = None

# DOC (binary) - 使用命令行工具解析，无需 textract

# CSV / Excel
try:
    import pandas as pd
except Exception:
    pd = None


def _parse_txt(data: bytes, encoding: Optional[str] = 'utf-8') -> str:
    """文本解码：指定编码优先，失败后走统一回退链（utf-8-sig/utf-8/gb18030/big5/latin-1）。

    此前失败直接 latin-1（会把 GBK 中文解成乱码）；改用回退链后
    GBK/GB2312/Big5 等常见编码的中文文本都能正确还原。
    """
    if encoding:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    text, _used = decode_text_bytes(data)
    return text


def _parse_md(data: bytes) -> str:
    return _parse_txt(data)


def _parse_pdf(data: bytes) -> str:
    """
    使用 PyMuPDF 解析 PDF 文件并提取所有文本
    参数:
        data: PDF 文件的二进制数据
    返回:
        提取的完整文本内容
    异常:
        RuntimeError: 当 PyMuPDF 不可用时抛出
    """
    if not fitz:
        raise RuntimeError('(fitz) PyMuPDF not installed or unavailable')
    try:
        # 从字节数据打开 PDF 文档
        doc = fitz.open(stream=data, filetype="pdf")
        texts = []
        # 遍历所有页面提取文本
        for page in doc:
            page_text = page.get_text("text")
            if page_text:
                texts.append(page_text)
        doc.close()
        return "\n\n".join(texts)
    except Exception as e:
        raise RuntimeError(f'PDF 解析失败：{str(e)}')


def _parse_pdf_with_pages(data: bytes) -> List[Dict]:
    """
    使用 PyMuPDF 解析 PDF 文件并按物理页面分割
    参数:
        data: PDF 文件的二进制数据
    返回:
        字典列表，每个字典包含:
        - content: 页面文本内容
        - page_number: 页码 (从 1 开始的整数)
    说明:
        PyMuPDF 按真实物理页面提取，避免了 pdfminer 的过度分割问题
        每页只提取有实际内容的文本，自动过滤空白页面
    """
    if not fitz:
        # PyMuPDF 不可用时回退到简单模式
        try:
            text = _parse_pdf(data)
            return [{"content": text, "page_number": None}]
        except Exception:
            return [{"content": "", "page_number": None}]
    try:
        # 从字节数据打开 PDF 文档
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        # 遍历所有页面
        for page_num in range(len(doc)):
            page = doc[page_num]
            # 提取页面文本
            page_text = page.get_text("text").strip()
            # 只添加有实际内容的页面
            if page_text:
                pages.append({
                    "content": page_text,
                    "page_number": page_num + 1  # 页码从 1 开始
                })
        doc.close()
        # 如果所有页面都为空，返回空内容
        return pages if pages else [{"content": "", "page_number": None}]
    except Exception as e:
        print(f"PDF 分页解析失败：{e}，回退到单文本模式")
        try:
            text = _parse_pdf(data)
            return [{"content": text, "page_number": None}]
        except Exception:
            return [{"content": "", "page_number": None}]


def _parse_docx(data: bytes) -> str:
    """
    使用 python-docx 解析 DOCX 文件并提取所有文本
    参数:
        data: DOCX 文件的二进制数据
    返回:
        提取的完整文本内容 (已过滤控制字符)
    异常:
        RuntimeError: 当 python-docx 不可用时抛出
    """
    if docx is None:
        raise RuntimeError('python-docx not installed or unavailable')
    try:
        with io.BytesIO(data) as bio:
            doc = Document(bio)
            texts = []
            # 遍历所有段落提取文本
            for p in doc.paragraphs:
                try:
                    # 获取段落文本
                    para_text = p.text
                    # 确保是字符串类型
                    if para_text:
                        # 过滤控制字符 (保留换行符和制表符)
                        cleaned_text = ''.join(
                            char for char in para_text 
                            if ord(char) >= 32 or char in '\n\r\t'
                        )
                        # 只添加非空文本
                        if cleaned_text.strip():
                            texts.append(cleaned_text.strip())
                except Exception as e:
                    # 跳过有问题的段落
                    print(f"警告：跳过段落解析错误 - {e}")
                    continue
            # 合并所有文本
            return "\n\n".join(texts) if texts else ""
    except Exception as e:
        raise RuntimeError(f'DOCX 解析失败：{str(e)}')


def _parse_docx_with_pages(data: bytes) -> List[Dict]:
    """
    解析 DOCX 文件 (DOCX 本身没有页码概念，返回 None)
    参数:
        data: DOCX 文件的二进制数据
    返回:
        字典列表，每个字典包含:
        - content: 文本内容 (已过滤控制字符)
        - page_number: None (DOCX 无页码概念)
    说明:
        DOCX 文件没有物理页面概念，返回整个文档内容作为单个"页面"
        已添加控制字符过滤，避免乱码问题
    """
    if docx is None:
        raise RuntimeError('python-docx not installed or unavailable')
    try:
        with io.BytesIO(data) as bio:
            doc = Document(bio)
            texts = []
            # 遍历所有段落提取文本
            for p in doc.paragraphs:
                try:
                    para_text = p.text
                    if para_text:
                        # 过滤控制字符 (保留换行符和制表符)
                        cleaned_text = ''.join(
                            char for char in para_text 
                            if ord(char) >= 32 or char in '\n\r\t'
                        )
                        if cleaned_text.strip():
                            texts.append(cleaned_text.strip())
                except Exception as e:
                    print(f"警告：跳过段落解析错误 - {e}")
                    continue
            # 合并所有文本
            content = "\n\n".join(texts) if texts else ""
            # DOCX 文件没有页码概念，返回 None
            return [{"content": content, "page_number": None}]
    except Exception as e:
        print(f"DOCX 分页解析失败：{e}，回退到简单模式")
        try:
            content = _parse_docx(data)
            return [{"content": content, "page_number": None}]
        except Exception:
            return [{"content": "", "page_number": None}]


def _parse_doc(data: bytes) -> str:
    # .doc (binary) 使用命令行工具解析
    # 1. 尝试使用antiword命令行工具
    try:
        with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            # 尝试运行antiword
            result = subprocess.run(
                ['antiword', tmp_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            # antiword不可用，继续尝试catdoc
            pass
        finally:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except:
                pass
    except Exception:
        # 临时文件创建失败，继续尝试其他方法
        pass
    # 2. 尝试使用catdoc命令行工具
    try:
        with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            # 尝试运行catdoc
            result = subprocess.run(
                ['catdoc', tmp_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            # catdoc也不可用
            pass
        finally:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except:
                pass
    except Exception:
        # 临时文件创建失败
        pass
    # 3. 所有方法都失败，提供详细的错误信息
    error_msg = """
无法解析.doc文件。请安装以下任一命令行工具：
1. 安装antiword命令行工具：
   - Ubuntu/Debian: sudo apt-get install antiword
   - macOS: brew install antiword
   - Windows: 下载 http://www.winfield.demon.nl/ 并添加到PATH
   
2. 安装catdoc命令行工具：
   - Ubuntu/Debian: sudo apt-get install catdoc
   - macOS: brew install catdoc
   - Windows: 下载 http://www.winfield.demon.nl/ 并添加到PATH
   
3. 或者将.doc文件转换为.docx格式后再上传
"""
    raise RuntimeError(error_msg)


def _parse_doc_with_pages(data: bytes) -> List[Dict]:
    """
    解析 DOC 文件（DOC 本身没有可靠的页码概念，返回 None）
    返回：包含 content 和 page_number 的字典列表
    """
    content = _parse_doc(data)
    return [{"content": content, "page_number": None}]


def _parse_csv(data: bytes) -> str:
    if pd is None:
        raise RuntimeError('pandas not installed')
    with io.BytesIO(data) as bio:
        bio.seek(0)
        try:
            df = pd.read_csv(bio, dtype=str, engine='python')
        except Exception:
            bio.seek(0)
            df = pd.read_csv(bio, dtype=str, encoding='utf-8', engine='python', on_bad_lines='skip')
        texts = []
        for r in df.fillna('').astype(str).values:
            texts.append(' '.join(r.tolist()))
        return '\n'.join(texts)


def _parse_csv_with_pages(data: bytes) -> List[Dict]:
    """
    解析 CSV 文件（无页码概念）
    返回：包含 content 和 page_number 的字典列表
    """
    content = _parse_csv(data)
    return [{"content": content, "page_number": None}]


def _parse_excel(data: bytes) -> str:
    if pd is None:
        raise RuntimeError('pandas not installed')
    with io.BytesIO(data) as bio:
        bio.seek(0)
        df = pd.read_excel(bio, sheet_name=None)
        texts = []
        for sheet, frame in df.items():
            texts.append(f"Sheet: {sheet}")
            for r in frame.fillna('').astype(str).values:
                texts.append(' '.join(r.tolist()))
        return '\n'.join(texts)


def _parse_excel_with_pages(data: bytes) -> List[Dict]:
    """
    解析 Excel 文件，每个 sheet 作为一个"页面"
    返回：包含 content 和 page_number 的字典列表
    """
    if pd is None:
        raise RuntimeError('pandas not installed')
    with io.BytesIO(data) as bio:
        bio.seek(0)
        df = pd.read_excel(bio, sheet_name=None)
        pages = []
        for i, (sheet, frame) in enumerate(df.items()):
            texts = []
            for r in frame.fillna('').astype(str).values:
                texts.append(' '.join(r.tolist()))
            content = f"Sheet: {sheet}\n" + '\n'.join(texts)
            pages.append({
                "content": content,
                "page_number": i + 1  # 使用 sheet 索引作为页码
            })
        return pages if pages else [{"content": "", "page_number": None}]


def _parse_txt_with_pages(data: bytes, encoding: Optional[str] = 'utf-8') -> List[Dict]:
    """
    解析 TXT 文件（TXT 文件没有页码概念，返回 None）
    返回：包含 content 和 page_number 的字典列表
    """
    if encoding:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text, _used = decode_text_bytes(data)
    else:
        text, _used = decode_text_bytes(data)
    # TXT 文件没有页码概念，直接返回整个内容，页码设为 None
    return [{"content": text, "page_number": None}]


def _parse_md_with_pages(data: bytes) -> List[Dict]:
    """
    解析 MD 文件（MD 文件没有页码概念，返回 None）
    返回：包含 content 和 page_number 的字典列表
    """
    text, _used = decode_text_bytes(data)
    # MD 文件没有页码概念，直接返回整个内容，页码设为 None
    return [{"content": text, "page_number": None}]

PARSER_BY_EXT = {
    '.txt': _parse_txt,
    '.md': _parse_md,
    '.pdf': _parse_pdf,
    '.docx': _parse_docx,
    '.doc': _parse_doc,
    '.csv': _parse_csv,
    '.xls': _parse_excel,
    '.xlsx': _parse_excel,
}

# ---------- 文本类文件：扩展名白名单 + 编码回退解码 + 二进制嗅探 ----------
# 上传链路的「文本类文件」口径（需求：文本文件直接上传，文本内容即解析结果）：
# - 已知可解析文档（pdf/docx/doc/csv/xls/xlsx）走各自解析器（PARSER_BY_EXT）；
# - 常见文本扩展名（代码/配置/日志/字幕/数据等）直接按文本解码，无需专用解析器；
# - 未知扩展名：先二进制嗅探（前 8KB 含空字节、或控制字符占比过高 → 拒绝），
#   通过则按文本解码。避免此前「未知类型一律 latin-1 硬解码」把二进制文件
#   解出满屏乱码文本塞进上下文。
TEXT_FILE_EXTENSIONS = frozenset({
    # 文档/笔记/数据
    '.txt', '.md', '.markdown', '.rst', '.log', '.text', '.me',
    '.csv', '.tsv', '.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.toml',
    '.ini', '.cfg', '.conf', '.properties', '.env', '.gitignore', '.gitattributes',
    '.editorconfig', '.srt', '.vtt', '.ass', '.tex', '.bib',
    # Web / 前端
    '.html', '.htm', '.xhtml', '.xml', '.xsl', '.xslt', '.css', '.scss', '.sass',
    '.less', '.styl', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue',
    '.svelte', '.map', '.svg',
    # 通用编程语言
    '.py', '.pyi', '.ipynb', '.java', '.kt', '.kts', '.scala', '.groovy',
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.cs', '.go', '.rs',
    '.swift', '.m', '.mm', '.php', '.rb', '.pl', '.pm', '.lua', '.r', '.jl',
    '.dart', '.ex', '.exs', '.erl', '.hrl', '.hs', '.clj', '.cljs', '.el',
    '.vim', '.asm', '.s', '.f', '.f90', '.f95', '.for', '.pas', '.d', '.nim',
    '.zig', '.v', '.sol', '.tcl', '.awk', '.sed',
    # 脚本 / 配置 / 构建
    '.sh', '.bash', '.zsh', '.fish', '.ps1', '.psm1', '.bat', '.cmd', '.sql',
    '.graphql', '.gql', '.proto', '.thrift', '.cmake', '.make', '.mk', '.gradle',
    '.dockerfile', '.containerfile', '.tf', '.tfvars', '.hcl', '.nix',
    # 其它纯文本
    '.diff', '.patch', '.po', '.pot', '.strings', '.pem',
    '.crt', '.cer', '.key', '.pub', '.asc', '.lic', '.license',
})
# 文本解码回退链：BOM 变体 → 常用 Unicode/中文编码 → 单字节兜底
_TEXT_DECODE_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")
# 二进制嗅探：样本大小与「非文本控制字符」占比阈值
_BINARY_SNIFF_SAMPLE_BYTES = 8192
_BINARY_CONTROL_RATIO = 0.10


def get_text_extension(filename: str) -> bool:
    """按扩展名判断是否属于常见文本类文件（不含 pdf/docx 等富文档）。"""
    _, ext = os.path.splitext(str(filename).lower())
    return ext in TEXT_FILE_EXTENSIONS


def is_probably_binary(data: bytes) -> bool:
    """二进制嗅探：前 8KB 出现空字节即视为二进制文件（与内置工具同口径）。

    另加控制字符占比判定：除 \\t\\n\\r\\f\\v 外的控制字符占比超过 10% 也视为
    二进制（覆盖无空字节的压缩/编码数据）。
    """
    sample = bytes(data[:_BINARY_SNIFF_SAMPLE_BYTES])
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(
        1 for byte in sample
        if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D, 0x0C, 0x0B)
    )
    return control / len(sample) > _BINARY_CONTROL_RATIO


def decode_text_bytes(data: bytes) -> tuple[str, str]:
    """按编码回退链把字节解码为文本，返回 (文本, 实际采用的编码)。

    回退链：utf-8-sig（带 BOM 的 UTF-8）→ utf-8 → gb18030（GBK/GB2312 超集）
    → big5 → latin-1（单字节兜底，永不失败）。
    """
    for candidate in _TEXT_DECODE_CANDIDATES:
        try:
            return data.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def parse_text_file_bytes(filename: str, data: bytes) -> str:
    """把文本类文件字节解析为文本内容（直接使用文本内容作为解析结果）。

    Raises:
        ValueError: 疑似二进制文件（扩展名未知且内容不像文本）时抛出，
        由上传链路转为该文件的失败反馈。
    """
    known_text = get_text_extension(filename)
    if is_probably_binary(data) and not known_text:
        raise ValueError(f"{filename} 疑似二进制文件（非文本内容），无法按文本解析")
    text, _encoding = decode_text_bytes(data)
    return text


# 带页码解析的映射表
PAGES_PARSER_BY_EXT = {
    '.txt': _parse_txt_with_pages,
    '.md': _parse_md_with_pages,
    '.pdf': _parse_pdf_with_pages,
    '.docx': _parse_docx_with_pages,
    '.doc': _parse_doc_with_pages,
    '.csv': _parse_csv_with_pages,
    '.xls': _parse_excel_with_pages,
    '.xlsx': _parse_excel_with_pages,
}


def get_parser_for_filename(filename: str):
    _, ext = os.path.splitext(filename.lower())
    return PARSER_BY_EXT.get(ext)


def get_pages_parser_for_filename(filename: str):
    """获取支持页码的解析器"""
    _, ext = os.path.splitext(filename.lower())
    return PAGES_PARSER_BY_EXT.get(ext)


def extract_text_from_bytes(filename: str, data: bytes) -> str:
    parser = get_parser_for_filename(filename)
    if not parser:
        # 未知扩展名 / 常见文本类：先二进制嗅探，通过则按编码回退链解码
        return parse_text_file_bytes(filename, data)
    return parser(data)


def extract_pages_from_bytes(filename: str, data: bytes) -> List[Dict]:
    """
    从文件字节数据中解析出带页码的页面内容
    参数:
        filename: 文件名（用于确定文件类型）
        data: 文件的二进制数据
    返回:
        字典列表，每个字典包含:
        - content: 页面内容
        - page_number: 页码（整数或 None）
    """
    parser = get_pages_parser_for_filename(filename)
    if not parser:
        # 未知扩展名 / 常见文本类：二进制嗅探 + 编码回退解码
        text = parse_text_file_bytes(filename, data)
        return [{"content": text, "page_number": None}]
    try:
        return parser(data)
    except Exception as e:
        print(f"文件解析失败：{filename}, 错误：{e}")
        # 尝试回退到简单文本解析
        try:
            text = _parse_txt(data)
            return [{"content": text, "page_number": None}]
        except Exception:
            return [{"content": "", "page_number": None}]


def batch_extract_text_from_bytes(
    file_data_list: List[Dict[str, bytes]],
    max_workers: int = 4
) -> List[Dict]:
    """
    并发批量解析多个文件的文本内容
    参数:
        file_data_list: 文件数据列表，每个元素是 {'filename': str, 'data': bytes} 的字典
        max_workers: 最大并发线程数，默认4
    返回:
        解析结果列表，每个元素是 {
            'filename': str,
            'status': 'success' | 'failed',
            'content': str (成功时),
            'error': str (失败时)
        } 的字典
    """
    results = []
    
    def _parse_single_file(file_info: Dict[str, bytes]) -> Dict:
        """解析单个文件的内部函数"""
        filename = file_info['filename']
        data = file_info['data']
        try:
            content = extract_text_from_bytes(filename, data)
            return {
                'filename': filename,
                'status': 'success',
                'content': content
            }
        except Exception as e:
            return {
                'filename': filename,
                'status': 'failed',
                'error': str(e)
            }
    
    # 使用线程池并发解析
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(_parse_single_file, file_info): file_info
            for file_info in file_data_list
        }
        for future in as_completed(future_to_file):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                file_info = future_to_file[future]
                results.append({
                    'filename': file_info['filename'],
                    'status': 'failed',
                    'error': f'线程执行异常: {str(e)}'
                })
    # 按原始顺序排序（可选）
    filename_order = {info['filename']: i for i, info in enumerate(file_data_list)}
    results.sort(key=lambda x: filename_order.get(x['filename'], 999))
    return results


def batch_extract_pages_from_bytes(
    file_data_list: List[Dict[str, bytes]],
    max_workers: int = 4
) -> List[Dict]:
    """
    并发批量解析多个文件的分页内容
    参数:
        file_data_list: 文件数据列表，每个元素是 {'filename': str, 'data': bytes} 的字典
        max_workers: 最大并发线程数，默认4
    返回:
        解析结果列表，每个元素是 {
            'filename': str,
            'status': 'success' | 'failed',
            'pages': List[Dict] (成功时),
            'error': str (失败时)
        } 的字典
    """
    results = []
    
    def _parse_single_file_pages(file_info: Dict[str, bytes]) -> Dict:
        """解析单个文件分页的内部函数"""
        filename = file_info['filename']
        data = file_info['data']
        try:
            pages = extract_pages_from_bytes(filename, data)
            return {
                'filename': filename,
                'status': 'success',
                'pages': pages
            }
        except Exception as e:
            return {
                'filename': filename,
                'status': 'failed',
                'error': str(e)
            }
    
    # 使用线程池并发解析
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(_parse_single_file_pages, file_info): file_info
            for file_info in file_data_list
        }
        for future in as_completed(future_to_file):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                file_info = future_to_file[future]
                results.append({
                    'filename': file_info['filename'],
                    'status': 'failed',
                    'error': f'线程执行异常: {str(e)}'
                })
    # 按原始顺序排序
    filename_order = {info['filename']: i for i, info in enumerate(file_data_list)}
    results.sort(key=lambda x: filename_order.get(x['filename'], 999))
    return results
