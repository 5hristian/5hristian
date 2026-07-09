import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

from PIL import Image, ImageOps

DEFAULT_AVATAR = '.github/workflows/Images/Avatar.png'
ASCII_COLS = 44
ASCII_ROWS = 25
ASCII_X = 15
ASCII_LEFT_MARGIN = 0
ASCII_Y_START = 30
ASCII_Y_STEP = 20
ART_WIDTH = 40
ART_MAX_CHARS = 38
RAMP = " .'-,:;!|j({[%kM%@g"
DARK_FLOOR = 46
GAMMA = 0.62

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
def auth_headers():
    token = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise KeyError(
            'ACCESS_TOKEN or GITHUB_TOKEN is required. '
            'Set one before running: $env:ACCESS_TOKEN = "ghp_..."'
        )
    return {'authorization': 'token ' + token}


USER_NAME = os.environ.get('USER_NAME', '5hristian')
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

# --- Profile card (edit these) ---
BIRTHDATE = datetime.datetime(2004, 11, 29)
PROFILE_X = 390
PROFILE_LINE_WIDTH = 58
HEADER_RULE_WIDTH = 58

PROFILE = {
    'header': 'christian@clipzy',
    'os': 'Windows 11, macOS',
    'host': 'clipzy.org',
    'kernel': 'Video Editor, Developer',
    'ide': 'Cursor 3.1.15',
    'programming': 'Node.js, Python, Lua, TypeScript',
    'computer': 'HTML, CSS, SQL, JSON, YAML',
    'real': 'English',
    'hobbies': [
        ('Software', 'Coding & Playing Games'),
        ('Creative', 'After Effects, Premiere, Photoshop'),
    ],
    'contact': [
        ('Twitter', None, '@clpzy'),
        ('Discord', None, 'clipzy.'),
        ('GitHub', None, '5hristian'),
        ('Email', 'Personal', 'contact@clipzy.org'),
    ],
}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


RETRYABLE_STATUS = {502, 503, 504}


def graphql_post(query, variables, retries=5):
    """POST to GitHub GraphQL with retries on transient gateway errors."""
    for attempt in range(retries):
        request = requests.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables},
            headers=auth_headers(),
        )
        if request.status_code == 200:
            return request
        if request.status_code in RETRYABLE_STATUS and attempt < retries - 1:
            time.sleep(min(30, 2 ** attempt))
            continue
        return request
    return request


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = graphql_post(query, variables)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
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
    query_count('recursive_loc')
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
    request = graphql_post(query, variables) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else: return 0
    force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


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
    query_count('loc_query')
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
                    print('   counting LOC:', edges[index]['node']['nameWithOwner'], flush=True)
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


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] # remove the comment block    
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

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


def center_ascii_block(cores, width):
    """Left-align the art block with a small margin, matching Andrew's layout."""
    centered = []
    for core in cores:
        if not core:
            centered.append(' ' * width)
        else:
            line = (' ' * ASCII_LEFT_MARGIN + core)[:width]
            centered.append(line.ljust(width)[:width])
    return centered


def ascii_art(path):
    """Convert avatar.png into centered ASCII rows for the SVG template."""
    img = Image.open(path).convert('L')
    img = ImageOps.autocontrast(img, cutoff=1)
    h = int(ART_WIDTH * img.height / img.width * 0.50)
    img = img.resize((ART_WIDTH, h))
    px = img.load()
    rows = []
    for y in range(h):
        row = ''
        for x in range(ART_WIDTH):
            v = px[x, y]
            if v < DARK_FLOOR:
                row += ' '
                continue
            lum = ((v - DARK_FLOOR) / (255 - DARK_FLOOR)) ** GAMMA
            row += RAMP[min(int(lum * len(RAMP)), len(RAMP) - 1)]
        row = row.rstrip()
        if len(row) > ART_MAX_CHARS:
            step = len(row) / ART_MAX_CHARS
            row = ''.join(row[min(int(i * step), len(row) - 1)] for i in range(ART_MAX_CHARS))
        rows.append(row)
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    stretched = []
    for i in range(ASCII_ROWS):
        if not rows:
            stretched.append('')
            continue
        src = round(i * (len(rows) - 1) / max(ASCII_ROWS - 1, 1))
        stretched.append(rows[src])
    return center_ascii_block(stretched, ASCII_COLS)


def ascii_overwrite(filename, art_rows):
    """Write ASCII art tspans into the fixed SVG template."""
    tree = etree.parse(filename)
    root = tree.getroot()
    ascii_node = root.find('.//*[@class="ascii"]')
    if ascii_node is None:
        raise ValueError('No ascii text node found in ' + filename)
    for child in list(ascii_node):
        ascii_node.remove(child)
    for i, row in enumerate(art_rows):
        y = ASCII_Y_START + i * ASCII_Y_STEP
        tspan = etree.SubElement(ascii_node, 'tspan')
        tspan.set('x', str(ASCII_X))
        tspan.set('y', str(y))
        tspan.text = row
        tspan.tail = '\n'
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def profile_dots(prefix, value):
    """Leader dots between a label and its value."""
    count = PROFILE_LINE_WIDTH - len(prefix) - len(value)
    if count < 1:
        count = 1
    return ' ' + ('.' * count) + ' '


def profile_header_rule(title):
    dash_count = max(10, HEADER_RULE_WIDTH - len(title) - 3)
    return ' -' + '—' * dash_count + '-—-'


def profile_add_blank(parent, y):
    row = etree.SubElement(parent, 'tspan')
    row.set('x', str(PROFILE_X))
    row.set('y', str(y))
    row.set('class', 'cc')
    row.text = '. '
    row.tail = '\n'


def profile_add_header(parent, y, title):
    row = etree.SubElement(parent, 'tspan')
    row.set('x', str(PROFILE_X))
    row.set('y', str(y))
    row.text = title
    row.tail = profile_header_rule(title) + '\n'


def profile_add_field(parent, y, keys, value, value_id=None, dots_id=None):
    """Add a dotted key: ..... value row."""
    prefix = '. '
    for index, key in enumerate(keys):
        if index > 0:
            prefix += '.'
        prefix += key
    prefix += ':'

    row = etree.SubElement(parent, 'tspan')
    row.set('x', str(PROFILE_X))
    row.set('y', str(y))
    row.set('class', 'cc')
    row.text = '. '

    prev = row
    for index, key in enumerate(keys):
        if index > 0:
            prev.tail = '.'
        key_node = etree.SubElement(parent, 'tspan')
        key_node.set('class', 'key')
        key_node.text = key
        prev = key_node

    colon = etree.SubElement(parent, 'tspan')
    colon.text = ':'

    dots = etree.SubElement(parent, 'tspan')
    dots.set('class', 'cc')
    if dots_id:
        dots.set('id', dots_id)
    dots.text = profile_dots(prefix, value)

    val = etree.SubElement(parent, 'tspan')
    val.set('class', 'value')
    if value_id:
        val.set('id', value_id)
    val.text = value
    val.tail = '\n'


def profile_add_stats(parent, header_y):
    profile_add_header(parent, header_y, '- GitHub Stats')

    def line_start(y):
        row = etree.SubElement(parent, 'tspan')
        row.set('x', str(PROFILE_X))
        row.set('y', str(y))
        row.set('class', 'cc')
        row.text = '. '
        return row

    def span(cls, text, elem_id=None):
        node = etree.SubElement(parent, 'tspan')
        if cls:
            node.set('class', cls)
        if elem_id:
            node.set('id', elem_id)
        node.text = text
        return node

    line_start(header_y + 20)
    span('key', 'Repos')
    span(None, ':')
    span('cc', ' .... ', 'repo_data_dots')
    span('value', '0', 'repo_data')
    span(None, ' {')
    span('key', 'Contributed')
    span(None, ': ')
    span('value', '0', 'contrib_data')
    span(None, '} | ')
    span('key', 'Stars')
    span(None, ':')
    span('cc', ' ........... ', 'star_data_dots')
    span('value', '0', 'star_data').tail = '\n'

    line_start(header_y + 40)
    span('key', 'Commits')
    span(None, ':')
    span('cc', ' ................. ', 'commit_data_dots')
    span('value', '0', 'commit_data')
    span(None, ' | ')
    span('key', 'Followers')
    span(None, ':')
    span('cc', ' ....... ', 'follower_data_dots')
    span('value', '0', 'follower_data').tail = '\n'

    line_start(header_y + 60)
    span('key', 'Lines of Code on GitHub')
    span(None, ':')
    span('cc', '. ', 'loc_data_dots')
    span('value', '0', 'loc_data')
    span(None, ' ( ')
    span('addColor', '0', 'loc_add')
    span('addColor', '++')
    span(None, ', ')
    span(None, ' ', 'loc_del_dots')
    span('delColor', '0', 'loc_del')
    span('delColor', '--')
    span(None, ' )').tail = '\n'


def profile_add_entries(parent, y, entries, group=None):
    """Add rows from (key1, key2, value) tuples; optional group prefix like Websites."""
    for key1, key2, value in entries:
        if group is not None:
            keys = [group, key1]
        elif key2 is None:
            keys = [key1]
        else:
            keys = [key1, key2]
        profile_add_field(parent, y, keys, value)
        y += 20
    return y


def profile_build_info(parent):
    """Rebuild the right-column profile from PROFILE."""
    y = 30
    profile_add_header(parent, y, PROFILE['header'])
    y += 20
    profile_add_field(parent, y, ['OS'], PROFILE['os'])
    y += 20
    profile_add_field(parent, y, ['Uptime'], '0 years, 0 months, 0 days', 'age_data', 'age_data_dots')
    y += 20
    profile_add_field(parent, y, ['Host'], PROFILE['host'])
    y += 20
    profile_add_field(parent, y, ['Kernel'], PROFILE['kernel'])
    y += 20
    profile_add_field(parent, y, ['IDE'], PROFILE['ide'])
    y += 20
    profile_add_blank(parent, y)
    y += 20
    profile_add_field(parent, y, ['Languages', 'Programming'], PROFILE['programming'])
    y += 20
    profile_add_field(parent, y, ['Languages', 'Computer'], PROFILE['computer'])
    y += 20
    profile_add_field(parent, y, ['Languages', 'Real'], PROFILE['real'])
    y += 20
    profile_add_blank(parent, y)
    y += 20
    for label, value in PROFILE['hobbies']:
        profile_add_field(parent, y, ['Hobbies', label], value)
        y += 20
    y = profile_add_entries(parent, y, PROFILE.get('websites', []), group='Websites')
    profile_add_blank(parent, y)
    y += 20
    profile_add_header(parent, y, '- Contact')
    y += 20
    y = profile_add_entries(parent, y, PROFILE['contact'])
    last_contact_y = y - 20
    profile_add_stats(parent, last_contact_y + 40)


def profile_overwrite(filename):
    """Write profile fields from PROFILE into the SVG template."""
    tree = etree.parse(filename)
    root = tree.getroot()
    text_node = root.find('.//*[@x="390"]')
    if text_node is None:
        raise ValueError('No profile text node found in ' + filename)
    for child in list(text_node):
        text_node.remove(child)
    profile_build_info(text_node)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    find_and_replace(root, 'age_data', age_data)
    find_and_replace(root, 'age_data_dots', profile_dots('. Uptime:', str(age_data)))
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
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


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Andrew Grant (Andrew6rant), 2022-2025
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, BIRTHDATE)
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # several repositories that I've contributed to have since been deleted.
    if OWNER_ID == {'id': 'MDQ6VXNlcjU3MzMxMTM0'}: # only calculate for user Andrew6rant
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    avatar = os.environ.get('AVATAR_PATH', DEFAULT_AVATAR)
    if os.path.isfile(avatar):
        art_rows = ascii_art(avatar)
        ascii_overwrite('dark_mode.svg', art_rows)
        ascii_overwrite('light_mode.svg', art_rows)

    profile_overwrite('dark_mode.svg')
    profile_overwrite('light_mode.svg')

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))