from bs4 import BeautifulSoup

html = """
<div class="description">
    <p>This is a paragraph.</p>
    <p><span>This</span> is another paragraph.</p>
</div>
"""
# 示例：提取所有<p>标签内的文本，并合并
soup = BeautifulSoup(html, 'lxml')
description = ' '.join([p.get_text(strip=True) for p in soup.select('p')])
print(description)

# with open(r"C:\Users\Administrator\Desktop\千帆AppBuilder-产品文档.html", 'r') as f:
#     html = f.read()
#     soup = BeautifulSoup(html, 'lxml')
#     description = ' '.join([p.get_text(strip=True) for p in soup.select('p')])
#     # 示例：提取所有<p>标签内的文本，并合并
#     description = ' '.join([p.get_text(strip=True) for p in soup.select('p')])
#     # description += ' '.join([li.get_text(strip=True) for li in soup.select('li')])
#     description += '\n\n'.join([li.get_text(strip=True) for li in soup.select('code')])
#     # 或者直接取整个容器的纯文本
#     desc_div = soup.find('div', class_='description')
#     if desc_div:
#         text = desc_div.get_text(separator='\n', strip=True)

#     print(description)



# from trafilatura import extract
# with open(r"C:\Users\Administrator\Desktop\千帆AppBuilder-产品文档.html", 'r') as f:
#     html = f.read()
# text = extract(html, include_comments=False, include_tables=True)
# print(text)