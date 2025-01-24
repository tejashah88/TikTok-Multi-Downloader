# Native imports
from concurrent import futures
from urllib3.util import Retry
import argparse
import json
import os
import re
import traceback

# 3rd-Party imports
from fake_useragent import UserAgent
from parsel import Selector
from requests.adapters import HTTPAdapter
from sqlitedict import SqliteDict
from tqdm import tqdm
import jmespath
import requests

parser = argparse.ArgumentParser(description="Multitok: A simple script that downloads TikTok videos concurrently.")
watermark_group = parser.add_mutually_exclusive_group()
parser.add_argument("--links", default="links.txt", help="The path to the .txt file that contains the TikTok links. (Default: links.txt)")
watermark_group.add_argument("--no-watermark", action="store_true", help="Download videos without watermarks. (Default)")
watermark_group.add_argument("--watermark", action="store_true", help="Download videos with watermarks.")
parser.add_argument("--workers", default=4, help="Number of concurrent downloads. (Default: 4)", type=int)
parser.add_argument("--api-version", choices=['v1', 'v2', 'v3'], default='v3', help="API version to use for downloading videos. (Default: v3)")
parser.add_argument("--save-metadata", action="store_true", help="Write video metadata to file if specified.")
parser.add_argument("--skip-existing", action="store_true", help="Skip downloading videos that already exist.")
parser.add_argument("--no-folders", action="store_true", help="Download all videos to the current directory without creating user folders.")
parser.add_argument("--output-dir", default=".", help="Specify the output directory for downloads. (Default: current directory)")
args = parser.parse_args()


tt_user_agent_gen = UserAgent(
    os=["Windows"],
    platforms=["desktop"],
)

content_user_agent_gen = UserAgent(
    os=["Windows", "Chrome OS", "Mac OS X"],
    platforms=["desktop"],
)


class UrlCache:
    def __init__(self, db_name):
        # Initialize database with auto-commiting and more efficiency
        self.db = SqliteDict(db_name, autocommit=True, outer_stack=False)

    def save(self, url):
        self.db[url] = True

    def contains(self, url):
        return url in self.db

    def close(self):
        self.db.close()


def extract_video_id(url):
    if 'vm.tiktok.com' in url:
        response = requests.get(url, headers={'User-Agent': tt_user_agent_gen.random})
        url = response.url

    username_pattern = r"@([A-Za-z0-9_.]+)"
    content_type_pattern = r"/(video|photo)/(\d+)"

    username_match = re.search(username_pattern, url)
    username = username_match.group(0)

    content_type_match = re.search(content_type_pattern, url)
    content_type = content_type_match.group(1)
    video_id = content_type_match.group(2)

    return username, video_id, content_type


def extract_metadata(url):
    response = requests.get(url, headers={'User-Agent': tt_user_agent_gen.random})
    html = Selector(response.text)
    account_data = json.loads(html.xpath('//*[@id="__UNIVERSAL_DATA_FOR_REHYDRATION__"]/text()').get())
    data = account_data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]

    expression = """
    {
        id: id,
        description: desc,
        createTime: createTime,
        video: video.{height: height, width: width, duration: duration, ratio: ratio, bitrate: bitrate, format: format, codecType: codecType, definition: definition},
        author: author.{id: id, uniqueId: uniqueId, nickname: nickname, signature: signature},
        music: music.{id: id, title: title, authorName: authorName, duration: duration},
        stats: stats,
        suggestedWords: suggestedWords,
        diversificationLabels: diversificationLabels,
        contents: contents[].{textExtra: textExtra[].{hashtagName: hashtagName}}
    }
    """

    parsed_data = jmespath.search(expression, data)
    return parsed_data


def mount_retry_logic_to_session(sess):
    retries = Retry(
        total = 10,
        backoff_factor = 1,
        status_forcelist = [429, 500, 502, 503, 504]
    )

    retry_adapter = HTTPAdapter(max_retries=retries)
    sess.mount('http://', retry_adapter)
    sess.mount('https://', retry_adapter)


def downloader(file_name, link, response, extension):
    file_size = int(response.headers.get("content-length", 0))
    username, _ , content_type = extract_video_id(link)
    username = username[1:]

    if args.no_folders:
        folder_name = args.output_dir
        file_name = f"{username}_{file_name}"
    else:
        folder_name = os.path.join(args.output_dir, username)

    if not os.path.exists(folder_name):
        os.makedirs(folder_name)
        print(f"Folder created: {folder_name}\n")

    file_path = os.path.join(folder_name, f"{file_name}.{extension}")

    if os.path.exists(file_path) and args.skip_existing:
        print(f"\033[93mSkipping\033[0m: {file_name}.{extension} (already exists)")
        return

    with open(file_path, 'wb') as file, tqdm(
        total=file_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
        bar_format='{percentage:3.0f}%|{bar:20}{r_bar}{desc}', colour='green', desc=f"[{file_name}]"
    ) as progress_bar:
        for data in response.iter_content(chunk_size=1024):
            size = file.write(data)
            progress_bar.update(size)

    if args.save_metadata and content_type != "photo":
        if args.no_folders:
            metadata_path = os.path.join(args.output_dir, "metadata")
        else:
            metadata_path = os.path.join(folder_name, "metadata")

        if not os.path.exists(metadata_path):
            os.makedirs(metadata_path)

        metadata = extract_metadata(link)
        metadata_file_path = os.path.join(metadata_path, f"{file_name}.json")

        with open(metadata_file_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=4, ensure_ascii=False)


def download_v3(link):
    headers = {
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept': '*/*',
        'Connection': 'keep-alive',
        'Content-Type': 'application/x-www-form-urlencoded',
        'HX-Current-URL': 'https://tiktokio.com/',
        'HX-Request': 'true',
        'HX-Target': 'tiktok-parse-result',
        'HX-Trigger': 'search-btn',
        'Origin': 'https://tiktokio.com',
        'Referer': 'https://tiktokio.com/',
        'User-Agent': content_user_agent_gen.random,
    }

    _, file_name, content_type = extract_video_id(link)

    with requests.Session() as sess:
        mount_retry_logic_to_session(sess)

        try:
            r = sess.get("https://tiktokio.com/", headers=headers)
            selector = Selector(text=r.text)
            prefix = selector.css('input[name="prefix"]::attr(value)').get()

            data = {
                'prefix': prefix,
                'vid': link,
            }

            response = requests.post('https://tiktokio.com/api/v1/tk-htmx', headers=headers, data=data)
            selector = Selector(text=response.text)

            if content_type == "video":
                download_link_index = 2 if args.watermark else 0
                all_download_links = selector.css('div.tk-down-link a::attr(href)').getall()

                if len(all_download_links) == 0:
                    raise Exception('Post is either private or removed.')

                download_link = all_download_links[download_link_index]
                response = sess.get(download_link, stream=True, headers=headers)
                downloader(file_name, link, response, extension="mp4")
            else:
                download_links = selector.xpath('//div[@class="media-box"]/img/@src').getall()

                for index, download_link in enumerate(download_links):
                    response = sess.get(download_link, stream=True, headers=headers)
                    downloader(f"{file_name}_{index}", link, response, extension="jpeg")
        except Exception as ex:
            return False, ex

    return True, None


def download_v2(link):
    headers = {
        'Connection': 'keep-alive',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://musicaldown.com',
        'Referer': 'https://musicaldown.com/en?ref=more',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': content_user_agent_gen.random,
    }

    _, file_name, content_type = extract_video_id(link)

    with requests.Session() as sess:
        mount_retry_logic_to_session(sess)

        try:
            r = sess.get("https://musicaldown.com/en", headers=headers)
            selector = Selector(text=r.text)

            token_a = selector.xpath('//*[@id="link_url"]/@name').get()
            token_b = selector.xpath('//*[@id="submit-form"]/div/div[1]/input[2]/@name').get()
            token_b_value = selector.xpath('//*[@id="submit-form"]/div/div[1]/input[2]/@value').get()

            data = {
                token_a: link,
                token_b: token_b_value,
                'verify': '1',
            }

            response = sess.post('https://musicaldown.com/download', headers=headers, data=data)
            selector = Selector(text=response.text)

            if content_type == "video":
                watermark = selector.xpath('/html/body/div[2]/div/div[2]/div[2]/a[3]/@href').get()
                no_watermark = selector.xpath('/html/body/div[2]/div/div[2]/div[2]/a[1]/@href').get()

                if watermark is None and no_watermark is None:
                    raise Exception('Post is either private or removed.')

                download_link = watermark if args.watermark else no_watermark
                response = sess.get(download_link, stream=True, headers=headers)
                downloader(file_name, link, response, extension="mp4")
            else:
                download_links = selector.xpath('//div[@class="card-image"]/img/@src').getall()

                for index, download_link in enumerate(download_links):
                    response = sess.get(download_link, stream=True, headers=headers)
                    downloader(f"{file_name}_{index}", link, response, extension="jpeg")
        except Exception as ex:
            return False, ex

    return True, None


def download_v1(link):
    headers = {
        'Connection': 'keep-alive',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://tmate.cc',
        'Referer': 'https://tmate.cc/',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': content_user_agent_gen.random,
    }

    _, file_name, content_type = extract_video_id(link)

    with requests.Session() as sess:
        mount_retry_logic_to_session(sess)

        try:
            response = sess.get("https://tmate.cc/", headers=headers)

            selector = Selector(response.text)
            token = selector.css('input[name="token"]::attr(value)').get()
            data = {'url': link, 'token': token}

            response = sess.post('https://tmate.cc/action', headers=headers, data=data).json()
            if response['error']:
                raise Exception('Post is either private or removed.')

            selector = Selector(text=response['data'])

            if content_type == "video":
                download_link_index = 2 if args.watermark else 0
                download_link = selector.css('.downtmate-right.is-desktop-only.right a::attr(href)').getall()[download_link_index]

                response = sess.get(download_link, stream=True, headers=headers)
                downloader(file_name, link, response, extension="mp4")
            else:
                download_links = selector.css('.card-img-top::attr(src)').getall()
                for index, download_link in enumerate(download_links):
                    response = sess.get(download_link, stream=True, headers=headers)
                    downloader(f"{file_name}_{index}", link, response, extension="jpeg")
        except Exception as ex:
            return False, ex

    return True, None


if __name__ == '__main__':
    with open(args.links, 'r', encoding='utf-8') as links:
        tiktok_links = links.read().strip().split('\n')

    download_functions = {
        'v1': download_v1,
        'v2': download_v2,
        'v3': download_v3,
    }

    download_function = download_functions.get(args.api_version)

    if download_function:
        url_cache = UrlCache('url_cache.db')

        # Create a processing function that wraps the downloader
        # function with exception handling and cache management
        def process_tt_link(link):
            if not url_cache.contains(link):
                success, exception = download_function(link)

                if success:
                    url_cache.save(link)
                else:
                    print(f"\033[91mError\033[0m: {link} - {str(exception)}")
                    traceback.print_exception(type(exception), exception, exception.__traceback__)

                    with open("errors.txt", 'a') as error_file:
                        error_file.write(f'{link} - {str(exception)}\n')

        with futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            executor.map(process_tt_link, tiktok_links)
