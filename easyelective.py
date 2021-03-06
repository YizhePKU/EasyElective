import sys
import yaml
import csv
import re
from collections import namedtuple
from time import sleep
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from captcha import recognize_captcha


class EasyElectiveException(Exception):
    """Base exception"""

    pass


class AuthenticationError(EasyElectiveException):
    """Wrong studentID or password"""

    pass


class NetworkError(EasyElectiveException):
    """Network failure, connection timeout, etc"""

    pass


class SessionExpiredError(EasyElectiveException):
    """Not logged in, caught cheating, etc"""

    pass


class IllegalOperationError(EasyElectiveException):
    """Course cannot be elected, because of e.g. conflict"""

    pass


# Data structure for a course
Course = namedtuple(
    "Course", ["name", "classID", "college", "max_slots", "used_slots", "elect_address"]
)

# Setup logger
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("info.log", level="INFO")
logger.add("debug.log", level="DEBUG")


def get_iaaa_token(appid, username, password, redir):
    logger.debug("Attempting to get iaaa token")
    url = "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do"
    form = dict(
        appid=appid,
        userName=username,
        password=password,
        randCode="",
        smsCode="",
        otpCode="",
        redirUrl=redir,
    )

    try:
        r = requests.post(url, data=form, timeout=5)
    except requests.exceptions.RequestException:
        raise NetworkError

    try:
        token = r.json()["token"]
    except KeyError:
        raise AuthenticationError("Failed to get IAAA token")
    logger.debug("Successfully got IAAA token")
    return token


def get_elective_session(username, password):
    logger.debug("Attempting to get elective session")
    session = requests.Session()

    # Fake referer and user agent
    session.headers.update(
        {
            "Referer": "http://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/help/HelpController.jpf",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/69.0.3497.100 Safari/537.36",
        }
    )

    # Pass username and password to IAAA, and get IAAA token
    appid = "syllabus"
    redir = "http://elective.pku.edu.cn/elective2008/agent4Iaaa.jsp/../ssoLogin.do"
    token = get_iaaa_token(appid, username, password, redir)

    # Pass IAAA token to elective.pku.edu.cn, and get elective session
    login_url = "http://elective.pku.edu.cn/elective2008/ssoLogin.do"
    try:
        resp = session.get(login_url, params={"token": token}, timeout=5)
        if resp.status_code != 200:
            raise AuthenticationError
        logger.debug("Successfully got elective session")
        return session
    except requests.exceptions.RequestException as e:
        raise NetworkError from e


def get_courses(session):
    """Return an list of courses in selection plan
    Only gets the first page of courses for simplicity.
    """

    logger.debug("Attempting to get courses")
    url_supply_cancel = "http://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/supplement/SupplyCancel.do"
    # page2 = "http://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/supplement/supplement.jsp?netui_pagesize=electableListGrid%3B50&netui_row=electableListGrid%3B50"

    courses = []

    try:
        resp = session.get(url_supply_cancel, timeout=5)
    except requests.exceptions.RequestException as e:
        logger.debug("Network error while trying to get course list")
        raise NetworkError from e

    try:
        # Parse HTML
        soup = BeautifulSoup(resp.text, features="html.parser")
        table = soup.find("table", class_="datagrid")
        items = table.find_all("tr", class_=re.compile("datagrid-(even|odd)"))
        for item in items:
            data = item.find_all("td", class_="datagrid")
            # Parse course info
            name = data[0].text
            classID = data[5].text
            college = data[6].text
            max_slots, used_slots = (int(s) for s in data[9].text.split("/"))
            base_url = "http://elective.pku.edu.cn"
            elect_address = urljoin(base_url, data[10].find("a")["href"])
            courses.append(
                Course(name, classID, college, max_slots, used_slots, elect_address)
            )
        return courses
    except (ValueError, AttributeError) as e:
        logger.info("Failed to parse course list", stack_info=True)
        raise SessionExpiredError from e


def solve_captcha(session):
    """Request captcha from elective and upload the correct answer"""

    logger.debug("Attempting to solve a captcha")
    # Request a new captcha
    request_captcha_url = "http://elective.pku.edu.cn/elective2008/DrawServlet"
    img_bytes = session.get(request_captcha_url).content

    # Recognize the captcha
    result = recognize_captcha(img_bytes)

    # Upload result to elective
    submit_url = "http://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/supplement/validate.do"
    resp = session.post(submit_url, data={"validCode": result}, timeout=5)

    # If failed, retry
    try:
        if resp.json()["valid"] != "2":
            solve_captcha(session)
    except ValueError:
        raise SessionExpiredError


def elect(session, course):
    """Attempt to elect a course"""

    logger.info(f"Attempting to elect {course.name}")
    # Solve a captcha
    solve_captcha(session)
    try:
        resp = session.get(course.elect_address)
        soup = BeautifulSoup(resp.text, features="html.parser")
        msg = soup.find(id="msgTips").text
    except (KeyError, AttributeError) as e:
        raise SessionExpiredError from e

    # TODO: detect failure precisely
    if "成功" in msg:
        logger.info(f"Successfully elected {course.name}")
    else:
        logger.info(f"Failed to elect {course.name}: {msg}")
        raise IllegalOperationError


@logger.catch
def main():
    logger.info("Easy elective, version 0.1")
    # Load config
    with open("config.yaml") as config_file:
        config = yaml.load(config_file, Loader=yaml.BaseLoader)
        username = config["studentID"]
        password = config["password"]
    # Load target courses
    with open("targets.csv", newline="") as courses_file:
        csv_reader = csv.DictReader(courses_file)
        targets = list(csv_reader)

    session_expired = True

    while targets:
        while session_expired:
            # Login into elective
            try:
                sess = get_elective_session(username, password)
                logger.info("Got elective session")
                session_expired = False
            except AuthenticationError:
                logger.critical(
                    "Authentication error. Please check your student ID and password"
                )
                sys.exit(1)
            except NetworkError:
                # Retry later
                logger.info("Failed to get elective session. Retrying...")
                sleep(10)

        logger.debug("Refreshing course list")
        try:
            courses = get_courses(sess)
            for target in targets:
                # Search courses that correspond to target name and classID
                # Convert classID to int before comparision
                search_result = [
                    course
                    for course in courses
                    if course.name == target["courseName"]
                    if int(course.classID) == int(target["classID"])
                    if course.college == target["college"]
                ]

                # Warn if no course correspond to target
                if not search_result:
                    logger.warning(
                        f"Target {target['courseName']} not found in election plan."
                    )
                    targets.remove(target)

                # Check if course is selectable
                for course in search_result:
                    if course.used_slots < course.max_slots:
                        logger.info(
                            f"Discovered a electable course: {course.name}, class {course.classID}, {course.max_slots}/{course.used_slots}"
                        )
                        elect(sess, course)
                        targets.remove(target)
        except SessionExpiredError:
            logger.warning(
                "Session has expired. Retrying..."
            )
            session_expired = True
        except IllegalOperationError:
            logger.warning(
                f"Illegal Operation detected. Ignoring target {target['courseName']}"
            )
            targets.remove(target)
        except NetworkError:
            # Retry
            logger.warning("Network error detected, retrying...")
        sleep(10)
    logger.info("No more targets available. Exiting...")


if __name__ == "__main__":
    main()
