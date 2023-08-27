"""从JavDB抓取数据"""
import os
import re
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from web.base import Request, resp2html
from web.exceptions import *
from core.func import *
from core.config import cfg
from core.datatype import MovieInfo, GenreMap
from core.chromium import get_browsers_cookies


# 初始化Request实例。使用scraper绕过CloudFlare后，需要指定网页语言，否则可能会返回其他语言网页，影响解析
request = Request(use_scraper=True)
request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.7,en;q=0.6,ja;q=0.5'

logger = logging.getLogger(__name__)
genre_map = GenreMap('data/genre_javdb.csv')
permanent_url = 'https://javdb.com'
if cfg.Network.proxy:
    base_url = permanent_url
else:
    base_url = cfg.ProxyFree.javdb


def get_html_wrapper(url):
    """包装外发的request请求并负责转换为可xpath的html，同时处理Cookies无效等问题"""
    global request, cookies_pool
    r = request.get(url, delay_raise=True)
    if r.status_code == 200:
        # 发生重定向可能仅仅是域名重定向，因此还要检查url以判断是否被跳转到了登录页
        if r.history and '/login' in r.url:
            # 仅在需要时去读取Cookies
            if 'cookies_pool' not in globals():
                try:
                    cookies_pool = get_browsers_cookies()
                except Exception as e:
                    logger.warning('获取JavDB的登录凭据时出错，你可能使用的是国内定制版等非官方Chrome系浏览器')
                    logger.debug(e, exc_info=True)
                    cookies_pool = []
            if len(cookies_pool) > 0:
                item = cookies_pool.pop()
                # 更换Cookies时需要创建新的request实例，否则cloudscraper会保留它内部第一次发起网络访问时获得的Cookies
                request = Request(use_scraper=True)
                request.cookies = item['cookies']
                cookies_source = (item['profile'], item['site'])
                logger.debug(f'未携带有效Cookies而发生重定向，尝试更换Cookies为: {cookies_source}')
                return get_html_wrapper(url)
            else:
                raise CredentialError('JavDB: 所有浏览器Cookies均已过期')
        elif r.history and 'pay' in r.url.split('/')[-1]:
            raise PermissionError(f"JavDB: 此资源被限制为仅VIP可见: '{r.history[0].url}'")
        else:
            html = resp2html(r)
            return html
    elif r.status_code in (403, 503):
        html = resp2html(r)
        code_tag = html.xpath("//span[@class='code-label']/span")
        error_code = code_tag[0].text if code_tag else None
        if error_code:
            if error_code == '1020':
                block_msg = f'JavDB: {r.status_code} 禁止访问: 站点屏蔽了来自日本地区的IP地址，请使用其他地区的代理服务器'
            else:
                block_msg = f'JavDB: {r.status_code} 禁止访问: {url} (Error code: {error_code})'
        else:
            block_msg = f'JavDB: {r.status_code} 禁止访问: {url}'
        raise SiteBlocked(block_msg)
    else:
        raise WebsiteError(f'JavDB: {r.status_code} 非预期状态码: {url}')


def get_user_info(site, cookies):
    """获取cookies对应的JavDB用户信息"""
    try:
        request.cookies = cookies
        html = request.get_html(f'https://{site}/users/profile')
    except Exception as e:
        logger.info('JavDB: 获取用户信息时出错')
        logger.debug(e, exc_info=1)
        return
    # 扫描浏览器得到的Cookies对应的临时域名可能会过期，因此需要先判断域名是否仍然指向JavDB的站点
    if 'JavDB' in html.text:
        email = html.xpath("//div[@class='user-profile']/ul/li[1]/span/following-sibling::text()")[0].strip()
        username = html.xpath("//div[@class='user-profile']/ul/li[2]/span/following-sibling::text()")[0].strip()
        return email, username
    else:
        logger.debug('JavDB: 域名已过期: ' + site)


def get_valid_cookies():
    """扫描浏览器，获取一个可用的Cookies"""
    # 经测试，Cookies所发往的域名不需要和登录时的域名保持一致，只要Cookies有效即可在多个域名间使用
    for d in cookies_pool:
        info = get_user_info(d['site'], d['cookies'])
        if info:
            return d['cookies']
        else:
            logger.debug(f"{d['profile']}, {d['site']}: Cookies无效")


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    # JavDB搜索番号时会有多个搜索结果，从中查找匹配番号的那个
    html = get_html_wrapper(f'{base_url}/search?q={movie.dvdid}')
    ids = list(map(str.lower, html.xpath("//div[@class='video-title']/strong/text()")))
    movie_urls = html.xpath("//a[@class='box']/@href")
    match_count = len([i for i in ids if i == movie.dvdid.lower()])
    if match_count == 0:
        raise MovieNotFoundError(__name__, movie.dvdid, ids)
    elif match_count == 1:
        index = ids.index(movie.dvdid.lower())
        new_url = movie_urls[index]
        try:
            html2 = get_html_wrapper(new_url)
        except PermissionError:
            # 不开VIP不让看，过分。决定榨出能获得的信息，毕竟有时候只有这里能找到标题和封面
            box = html.xpath("//a[@class='box']")[index]
            movie.url = new_url
            movie.title = box.get('title')
            movie.cover = box.xpath("div/img/@src")[0]
            score_str = box.xpath("div[@class='score']/span/span")[0].tail
            score = re.search(r'([\d.]+)分', score_str).group(1)
            movie.score = "{:.2f}".format(float(score)*2)
            movie.publish_date = box.xpath("div[@class='meta']/text()")[0].strip()
            return
    else:
        raise MovieDuplicateError(__name__, movie.dvdid, match_count)

    container = html2.xpath("/html/body/section/div/div[@class='video-detail']")[0]
    info = container.xpath("//nav[@class='panel movie-panel-info']")[0]
    title = container.xpath("h2/strong[@class='current-title']/text()")[0]
    cover = container.xpath("//img[@class='video-cover']/@src")[0]
    preview_pics = container.xpath("//a[@class='tile-item'][@data-fancybox='gallery']/@href")
    preview_video_tag = container.xpath("//video[@id='preview-video']/source/@src")
    if preview_video_tag:
        preview_video = preview_video_tag[0]
        if preview_video.startswith('//'):
            preview_video = 'https:' + preview_video
        movie.preview_video = preview_video
    dvdid = info.xpath("div/span")[0].text_content()
    publish_date = info.xpath("div/strong[text()='日期:']")[0].getnext().text
    duration = info.xpath("div/strong[text()='時長:']")[0].getnext().text.replace('分鍾', '').strip()
    director_tag = info.xpath("div/strong[text()='導演:']")
    if director_tag:
        movie.director = director_tag[0].getnext().text_content().strip()
    producer_tag = info.xpath("div/strong[text()='片商:']")
    if producer_tag:
        movie.producer = producer_tag[0].getnext().text_content().strip()
    publisher_tag = info.xpath("div/strong[text()='發行:']")
    if publisher_tag:
        movie.publisher = publisher_tag[0].getnext().text_content().strip()
    serial_tag = info.xpath("div/strong[text()='系列:']")
    if serial_tag:
        movie.serial = serial_tag[0].getnext().text
    score_tag = info.xpath("//span[@class='score-stars']")
    if score_tag:
        score_str = score_tag[0].tail
        score = re.search(r'([\d.]+)分', score_str).group(1)
        movie.score = "{:.2f}".format(float(score)*2)
    genre_tags = info.xpath("//strong[text()='類別:']/../span/a")
    genre, genre_id = [], []
    for tag in genre_tags:
        pre_id = tag.get('href').split('/')[-1]
        genre.append(tag.text)
        genre_id.append(pre_id)
        # 判定影片有码/无码
        subsite = pre_id.split('?')[0]
        movie.uncensored = {'uncensored': True, 'tags':False}.get(subsite)
    # JavDB目前同时提供男女优信息，根据用来标识性别的符号筛选出女优
    actors_tag = info.xpath("//strong[text()='演員:']/../span")[0]
    all_actors = actors_tag.xpath("a/text()")
    genders = actors_tag.xpath("strong/text()")
    actress = [i for i in all_actors if genders[all_actors.index(i)] == '♀']
    magnet = container.xpath("//div[@class='magnet-name column is-four-fifths']/a/@href")

    movie.dvdid = dvdid
    movie.url = new_url.replace(base_url, permanent_url)
    movie.title = title.replace(dvdid, '').strip()
    movie.cover = cover
    movie.preview_pics = preview_pics
    movie.publish_date = publish_date
    movie.duration = duration
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress
    movie.magnet = [i.replace('[javdb.com]','') for i in magnet]


def parse_clean_data(movie: MovieInfo):
    """解析指定番号的影片数据并进行清洗"""
    try:
        parse_data(movie)
    except SiteBlocked:
        raise
        logger.error('JavDB: 可能触发了反爬虫机制，请稍后再试')
    if movie.genre_id:
        movie.genre_norm = genre_map.map(movie.genre_id)
        movie.genre_id = None   # 没有别的地方需要再用到，清空genre id（表明已经完成转换）


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('FC2-3189680')
    try:
        parse_clean_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
