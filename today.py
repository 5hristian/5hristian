import datetime
from dateutil import relativedelta
import requests
import os
import sys
from html import escape
import hashlib

from PIL import Image, ImageOps

def auth_headers():
    token = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise KeyError('ACCESS_TOKEN or GITHUB_TOKEN is required')
    return {'authorization': 'token ' + token}

USER_NAME = os.environ.get('USER_NAME', '5hristian')


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=auth_headers())
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text)


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return my total repository or star count.
    """
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=auth_headers()) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else: return 0
    force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text)

def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """Returns the account ID used to attribute commits in LOC counting."""
    query = '''
    query($login: String!){
        user(login: $login) {
            id
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def collect_stats():
    """
    Fetch live GitHub stats for the profile card generator.
    Returns repo, contribution, star, commit, follower, and LOC counts.
    """
    global OWNER_ID
    OWNER_ID = user_getter(USER_NAME)
    total_loc = loc_query(['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    commit_data = commit_counter(7)
    star_data = graph_repos_stars('stars', ['OWNER'])
    repo_data = graph_repos_stars('repos', ['OWNER'])
    contrib_data = graph_repos_stars('repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data = follower_getter(USER_NAME)
    return {
        'repos': repo_data,
        'contributed': contrib_data,
        'stars': star_data,
        'commits': commit_data,
        'followers': follower_data,
        'loc_add': total_loc[0],
        'loc_del': total_loc[1],
    }


# ---------------------------------------------------------------- profile card (ASCII avatar + SVG)

BIRTHDATE = datetime.date(2004, 11, 29)  # used for Uptime (age) — edit to your birthday

STATS = {
    "repos": 0,
    "contributed": 0,
    "stars": 0,
    "commits": 0,
    "followers": 0,
    "loc_add": 0,
    "loc_del": 0,
}

ART_WIDTH = 48
INFO_WIDTH = 62
GAP = "    "

FONT_SIZE = 16
CHAR_W = 9.65
LINE_H = 20
PAD_X, PAD_Y = 28, 26
SCALE = 1.18

THEMES = {
    "dark": {
        "bg": "#161b22", "border": "#30363d",
        "fg": "#c9d1d9", "key": "#ffa657", "val": "#79c0ff",
        "dots": "#8b949e", "art": "#c9d1d9",
        "green": "#56d364", "red": "#f85149",
    },
    "light": {
        "bg": "#fffefe", "border": "#d0d7de",
        "fg": "#24292f", "key": "#953800", "val": "#0969da",
        "dots": "#6e7781", "art": "#24292f",
        "green": "#1a7f37", "red": "#cf222e",
    },
}

RAMP = " .'-,:;!|j({[%kM%@g"
DARK_FLOOR = 46
GAMMA = 0.62
DEFAULT_AVATAR = ".github/workflows/Images/Avatar.png"


def ascii_art(path):
    img = Image.open(path).convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    h = int(ART_WIDTH * img.height / img.width * 0.5)
    img = img.resize((ART_WIDTH, h))
    px = img.load()
    rows = []
    for y in range(h):
        row = ""
        for x in range(ART_WIDTH):
            v = px[x, y]
            if v < DARK_FLOOR:
                row += " "
                continue
            lum = ((v - DARK_FLOOR) / (255 - DARK_FLOOR)) ** GAMMA
            row += RAMP[min(int(lum * len(RAMP)), len(RAMP) - 1)]
        rows.append(row.rstrip())
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def stretch_art(art, target_rows):
    """Vertically sample art to match info height without changing width."""
    if target_rows <= 0:
        return []
    if not art:
        return [""] * target_rows
    if len(art) == target_rows:
        return art
    if target_rows == 1:
        return [art[len(art) // 2]]
    stretched = []
    for i in range(target_rows):
        src = round(i * (len(art) - 1) / (target_rows - 1))
        stretched.append(art[src])
    return stretched


def format_plural(unit):
    return 's' if unit != 1 else ''


def uptime():
    diff = relativedelta.relativedelta(datetime.datetime.today(), BIRTHDATE)
    cake = ' 🎂' if diff.months == 0 and diff.days == 0 else ''
    return (
        f"{diff.years} year{format_plural(diff.years)}, "
        f"{diff.months} month{format_plural(diff.months)}, "
        f"{diff.days} day{format_plural(diff.days)}{cake}"
    )


def kv(key, value, key_cls="key", val_cls="val"):
    used = 2 + len(key) + 1 + 1 + 1 + len(value)
    dots = "." * max(INFO_WIDTH - used, 2)
    return [
        (". ", "dots"), (f"{key}:", key_cls), (" ", "fg"),
        (dots, "dots"), (" ", "fg"), (value, val_cls),
    ]


def rule(label=""):
    if label:
        head = f"─ {label} "
        return [(head + "─" * (INFO_WIDTH - len(head)), "fg")]
    return [("", "fg")]


def header():
    name = "christian@clipzy"
    return [(name, "bold"), (" " + "─" * (INFO_WIDTH - len(name) - 1), "fg")]


def stat_pair(k1, v1, k2, v2, split):
    left_used = 2 + len(k1) + 1 + 1 + 1 + len(v1)
    d1 = "." * max(split - left_used, 2)
    right_used = 3 + len(k2) + 1 + 1 + 1 + len(v2)
    d2 = "." * max(INFO_WIDTH - split - right_used, 2)
    return [
        (". ", "dots"), (f"{k1}:", "key"), (" ", "fg"), (d1, "dots"),
        (" ", "fg"), (v1, "val"), (" | ", "fg"), (f"{k2}:", "key"),
        (" ", "fg"), (d2, "dots"), (" ", "fg"), (v2, "val"),
    ]


def loc_line():
    s = STATS
    net = f"{s['loc_add'] - s['loc_del']:,}"
    add = f"{s['loc_add']:,}++"
    dele = f"{s['loc_del']:,}--"
    key = "Lines of Code on GitHub:"
    used = 2 + len(key) + 1 + len(net) + 3 + len(add) + 2 + len(dele) + 2
    dots = "." * max(INFO_WIDTH - used, 1)
    return [
        (". ", "dots"), (key, "key"), (dots, "dots"), (" ", "fg"),
        (net, "val"), (" ( ", "fg"), (add, "green"), (", ", "fg"),
        (dele, "red"), (" )", "fg"),
    ]


def info_lines():
    s = STATS
    return [
        header(),
        kv("OS", "Windows 11, macOS"),
        kv("Uptime", uptime()),
        kv("Host", "clipzy.org"),
        kv("Kernel", "Video Editor, Developer"),
        kv("IDE", "VS Code, Cursor"),
        [(".", "dots")],
        kv("Languages.Programming", "TypeScript, Python, Lua, Node.js"),
        kv("Languages.Computer", "HTML, CSS, SQL, JSON, YAML"),
        kv("Languages.Real", "English"),
        [(".", "dots")],
        kv("Hobbies.Software", "Coding & Playing Games"),
        kv("Hobbies.Creative", "After Effects, Premiere, Photoshop"),
        [("", "fg")],
        rule("Contact"),
        kv("Twitter", "@clpzy"),
        kv("Discord", "clipzy."),
        kv("GitHub", "5hristian"),
        kv("Email.Personal", "contact@clipzy.org"),
        [("", "fg")],
        rule("GitHub Stats"),
        stat_pair("Repos", f"{s['repos']} {{Contributed: {s['contributed']}}}",
                  "Stars", str(s["stars"]), split=40),
        stat_pair("Commits", f"{s['commits']:,}",
                  "Followers", str(s["followers"]), split=40),
        loc_line(),
    ]


def build_svg(theme, art):
    info = info_lines()
    n = len(info)
    art = stretch_art(art, n)
    rows = []
    for i in range(n):
        segs = []
        segs.append((art[i].ljust(ART_WIDTH) + GAP, "art"))
        segs.extend(info[i])
        rows.append(segs)

    cols = ART_WIDTH + len(GAP) + INFO_WIDTH
    width = round(cols * CHAR_W + 2 * PAD_X)
    height = n * LINE_H + 2 * PAD_Y

    t = theme
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{round(width * SCALE)}" height="{round(height * SCALE)}" '
        f'viewBox="0 0 {width} {height}" font-family="Menlo, Consolas, \'DejaVu Sans Mono\', monospace" '
        f'font-size="{FONT_SIZE}px">',
        "<style>",
        f".fg{{fill:{t['fg']}}} .key{{fill:{t['key']}}} .val{{fill:{t['val']}}}",
        f".dots{{fill:{t['dots']}}} .art{{fill:{t['art']}}}",
        f".green{{fill:{t['green']}}} .red{{fill:{t['red']}}}",
        f".bold{{fill:{t['fg']};font-weight:600}}",
        "</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>',
    ]
    for i, segs in enumerate(rows):
        y = PAD_Y + (i + 1) * LINE_H - 5
        spans = "".join(
            f'<tspan class="{cls}">{escape(txt)}</tspan>' for txt, cls in segs if txt
        )
        out.append(f'<text x="{PAD_X}" y="{y}" xml:space="preserve">{spans}</text>')
    out.append("</svg>")
    return "\n".join(out)


def resolve_avatar_path(arg):
    if arg is None:
        return DEFAULT_AVATAR
    if os.path.basename(arg).lower() == "avatar.png" and not os.path.isfile(arg):
        return DEFAULT_AVATAR
    if not os.path.isfile(arg):
        raise FileNotFoundError(f"Avatar not found: {arg}")
    return arg


def generate_profile_svgs(avatar_path):
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            STATS.update(collect_stats())
            print("stats: live")
        except Exception as e:
            print(f"stats fetch failed ({e}), using fallback STATS", file=sys.stderr)
    else:
        print("stats: fallback snapshot")

    art = ascii_art(avatar_path)
    for name, theme in THEMES.items():
        path = f"{name}_mode.svg"
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_svg(theme, art))
        print(f"wrote {path}")


if __name__ == '__main__':
    avatar = resolve_avatar_path(sys.argv[1] if len(sys.argv) > 1 else None)
    generate_profile_svgs(avatar)