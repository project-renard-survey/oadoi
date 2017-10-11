#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import re
import datetime
import sys
import requests
import random
from time import time
from Levenshtein import ratio
from collections import defaultdict
from HTMLParser import HTMLParser
from sqlalchemy import sql
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
import shortuuid

from app import db
from app import logger
from webpage import WebpageInBaseRepo
from webpage import WebpageInPmhRepo
from oa_local import find_normalized_license
from oa_pdf import convert_pdf_to_txt
from util import elapsed
from util import normalize
from util import remove_punctuation


DEBUG_BASE = False


class BaseResponseAddin():

    def get_good_urls(self):
        valid_urls = []

        # pmc can only add pmc urls.  otherwise has junk about dois that aren't actually open.
        if self.urls:
            if "oai:pubmedcentral.nih.gov" in self.id:
                for url in self.urls:
                    if "/pmc/" in url and url != "http://www.ncbi.nlm.nih.gov/pmc/articles/PMC":
                        pmcid_matches = re.findall(".*(PMC\d+).*", url)
                        if pmcid_matches:
                            pmcid = pmcid_matches[0]
                            url = u"https://www.ncbi.nlm.nih.gov/pmc/articles/{}".format(pmcid)
                            valid_urls.append(url)
            else:
                valid_urls += self.urls

        # filter out doi urls unless they are the only url
        # might be a figshare url etc, but otherwise is usually to a publisher page which
        # may or may not be open, and we are handling through hybrid path
        if len(valid_urls) > 1:
            valid_urls = [url for url in valid_urls if u"doi.org/" not in url]

        # filter out some urls that we know are closed
        blacklist_url_snippets = [
            u"/10.1093/analys/",
            u"academic.oup.com/analysis",
            u"analysis.oxfordjournals.org/",
            u"ncbi.nlm.nih.gov/pubmed/",
            u"gateway.webofknowledge.com/"
        ]
        for url_snippet in blacklist_url_snippets:
            valid_urls = [url for url in valid_urls if url_snippet not in url]


        # oxford IR doesn't return URLS, instead it returns IDs from which we can build URLs
        # example: https://www.base-search.net/Record/5c1cf4038958134de9700b6144ae6ff9e78df91d3f8bbf7902cb3066512f6443/
        if self.sources and "Oxford University Research Archive (ORA)" in self.sources:
            if self.relation:
                for relation in self.relation:
                    if relation.startswith("uuid"):
                        valid_urls += [u"https://ora.ox.ac.uk/objects/{}".format(relation)]

        # and then html unescape them, because some are html escaped
        h = HTMLParser()
        valid_urls = [h.unescape(url) for url in valid_urls]

        return valid_urls



    def make_external_locations(self, my_pub, match_type, do_scrape=True):
        external_locations = []

        for url in self.get_good_urls():
            my_external_location = ExternalLocation()
            my_external_location.id = self.id
            my_external_location.url = url
            my_external_location.doi = my_pub.doi

            my_external_location.scrape_updated = datetime.datetime.utcnow().isoformat()

            if "oai:pubmedcentral.nih.gov" in self.id:
                my_external_location.scrape_metadata_url = url
                my_external_location.scrape_pdf_url = u"{}/pdf".format(url)
            if "oai:arXiv.org" in self.id:
                my_external_location.scrape_metadata_url = url
                my_external_location.scrape_pdf_url = u"{}/pdf".format(url)
            if "oai:CiteSeerX.psu:" in self.id:
                my_external_location.scrape_metadata_url = url
                my_external_location.scrape_pdf_url = None

            for base_match in my_pub.base_matches:
                if url==base_match.scrape_metadata_url or url==base_match.scrape_pdf_url:
                    my_external_location.scrape_updated = base_match.scrape_updated
                    my_external_location.scrape_pdf_url = base_match.scrape_pdf_url
                    my_external_location.scrape_metadata_url = base_match.scrape_metadata_url
                    my_external_location.scrape_license = base_match.scrape_license
                    my_external_location.scrape_version = base_match.scrape_version

            if my_external_location.scrape_metadata_url or my_external_location.scrape_pdf_url:
                my_external_location.scrape_evidence = u"oa repository (via OAI-PMH {} match)".format(match_type)

            # license = find_normalized_license(self.license)
            # my_external_location.scrape_license = None
            # if do_scrape:
            #     my_external_location.scrape_version = my_external_location.find_version()

            external_locations.append(my_external_location)

        return external_locations


class PmhRecordMatchedByTitle(db.Model, BaseResponseAddin):
    id = db.Column(db.Text, db.ForeignKey('pmh_record.id'), primary_key=True)
    doi = db.Column(db.Text)
    title = db.Column(db.Text)
    normalized_title = db.Column(db.Text, db.ForeignKey('crossref_title_view.normalized_title'))
    urls = db.Column(JSONB)
    authors = db.Column(JSONB)
    relations = db.Column(JSONB)
    sources = db.Column(JSONB)


class ExternalLocation(db.Model):
    id = db.Column(db.Text, primary_key=True)
    doi = db.Column(db.Text, db.ForeignKey('crossref.id'))
    url = db.Column(db.Text)

    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_version = db.Column(db.Text)
    scrape_license = db.Column(db.Text)

    error = db.Column(db.Text)
    updated = db.Column(db.DateTime)

    def __init__(self, **kwargs):
        self.error = ""
        self.updated = datetime.datetime.utcnow().isoformat()
        super(self.__class__, self).__init__(**kwargs)

    @property
    def is_open(self):
        return (self.scrape_evidence and self.scrape_evidence != "closed")

    def find_version(self):
        if not self.scrape_pdf_url:
            return None

        try:
            text = convert_pdf_to_txt(self.scrape_pdf_url)
            # logger.info(text)
            if text:
                patterns = [
                    re.compile(ur"©.?\d{4}", re.UNICODE),
                    re.compile(ur"copyright \d{4}", re.IGNORECASE),
                    re.compile(ur"all rights reserved", re.IGNORECASE),
                    re.compile(ur"This article is distributed under the terms of the Creative Commons", re.IGNORECASE),
                    re.compile(ur"this is an open access article", re.IGNORECASE)
                    ]
                for pattern in patterns:
                    matches = pattern.findall(text)
                    if matches:
                        return "publishedVersion"
        except Exception as e:
            self.error += u"Exception doing convert_pdf_to_txt on {}! investigate! {}".format(self.scrape_pdf_url, unicode(e.message).encode("utf-8"))
            logger.info(self.error)

        return None


    def scrape_for_fulltext(self):
        self.set_webpages()
        response_webpages = []

        found_open_fulltext = False
        for my_webpage in self.webpages:
            if not found_open_fulltext:
                my_webpage.scrape_for_fulltext_link()
                if my_webpage.has_fulltext_url:
                    logger.info(u"** found an open copy! {}".format(my_webpage.fulltext_url))
                    found_open_fulltext = True
                    response_webpages.append(my_webpage)

        self.open_webpages = response_webpages
        sys.exc_clear()  # someone on the internet said this would fix All The Memory Problems. has to be in the thread.
        return self


    def set_fulltext_urls(self):

        self.fulltext_urls = []
        self.fulltext_license = None

        # first set license if there is one originally.  overwrite it later if scraped a better one.
        if "license" in self.doc and self.doc["license"]:
            self.fulltext_license = find_normalized_license(self.doc["license"])

        for my_webpage in self.open_webpages:
            if my_webpage.has_fulltext_url:
                response = {}
                # logger.info(u"setting self.fulltext_urls")
                self.fulltext_urls += [{"free_pdf_url": my_webpage.scraped_pdf_url, "pdf_landing_page": my_webpage.url}]
                if not self.fulltext_license or self.fulltext_license == "unknown":
                    self.fulltext_license = my_webpage.scraped_license
            else:
                logger.info(u"{} has no fulltext url alas".format(my_webpage))

        if self.fulltext_license == "unknown":
            self.fulltext_license = None

        # logger.info(u"set self.fulltext_urls to {}".format(self.fulltext_urls))



class PmhRecord(db.Model, BaseResponseAddin):
    id = db.Column(db.Text, primary_key=True)
    source = db.Column(db.Text)
    doi = db.Column(db.Text, db.ForeignKey('crossref.id'))
    record_timestamp = db.Column(db.DateTime)
    api_raw = db.Column(JSONB)
    title = db.Column(db.Text)
    license = db.Column(db.Text)
    oa = db.Column(db.Text)
    urls = db.Column(JSONB)
    authors = db.Column(JSONB)
    relations = db.Column(JSONB)
    sources = db.Column(JSONB)
    updated = db.Column(db.DateTime)

    def __init__(self, **kwargs):
        self.updated = datetime.datetime.utcnow().isoformat()
        super(self.__class__, self).__init__(**kwargs)


# legacy, just used for matching
class BaseMatch(db.Model):
    id = db.Column(db.Text, primary_key=True)
    base_id = db.Column(db.Text)
    doi = db.Column(db.Text, db.ForeignKey('crossref.id'))
    url = db.Column(db.Text)
    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_version = db.Column(db.Text)
    scrape_license = db.Column(db.Text)
    updated = db.Column(db.DateTime)

    @property
    def is_open(self):
        return (self.scrape_evidence and self.scrape_evidence != "closed")





def title_is_too_common(normalized_title):
    # these common titles were determined using this SQL,
    # which lists the titles of BASE hits that matched titles of more than 2 articles in a sample of 100k articles.
    # ugly sql, i know.  but better to include here as a comment than not, right?
    #     select norm_title, count(*) as c from (
    #     select id, response_jsonb->>'free_fulltext_url' as url, api->'_source'->>'title' as title, normalize_title_v2(api->'_source'->>'title') as norm_title
    #     from crossref where response_jsonb->>'free_fulltext_url' in
    #     ( select url from (
    #     select response_jsonb->>'free_fulltext_url' as url, count(*) as c
    #     from crossref
    #     where crossref.response_jsonb->>'free_fulltext_url' is not null
    #     and id in (select id from dois_random_articles_1mil_do_hybrid_100k limit 100000)
    #     group by url
    #     order by c desc) s where c > 1 ) limit 1000 ) ss group by norm_title order by c desc
    # and then have added more to it

    common_title_string = """
    informationreaders
    editorialboardpublicationinformation
    insidefrontcovereditorialboard
    graphicalcontentslist
    instructionsauthors
    reviewsandnoticesbooks
    editorialboardaimsandscope
    contributorsthisissue
    parliamentaryintelligence
    editorialadvisoryboard
    informationauthors
    instructionscontributors
    royalsocietymedicine
    guesteditorsintroduction
    cumulativesubjectindexvolumes
    acknowledgementreviewers
    medicalsocietylondon
    ouvragesrecuslaredaction
    royalmedicalandchirurgicalsociety
    moderntechniquetreatment
    reviewcurrentliterature
    answerscmeexamination
    publishersannouncement
    cumulativeauthorindex
    abstractsfromcurrentliterature
    booksreceivedreview
    royalacademymedicineireland
    editorialsoftwaresurveysection
    cumulativesubjectindex
    acknowledgementreferees
    specialcorrespondence
    atmosphericelectricity
    classifiedadvertising
    softwaresurveysection
    abstractscurrentliterature
    britishmedicaljournal
    veranstaltungskalender
    internationalconference
    """

    for common_title in common_title_string.split("\n"):
        if normalized_title==common_title.strip():
            return True
    return False





def refresh_external_locations(my_pub, do_scrape=True):
    external_locations = []

    start_time = time()

    if not my_pub:
        return

    for pmh_record_obj in my_pub.pmh_record_doi_links:
        # if do_scrape:
        #     pmh_record_obj.find_fulltext()
        match_type = "doi"
        external_locations += pmh_record_obj.make_external_locations(my_pub, match_type, do_scrape=do_scrape)

    if not my_pub.normalized_title:
        # logger.info(u"title '{}' is too short to match BASE by title".format(my_pub.best_title))
        return

    if title_is_too_common(my_pub.normalized_title):
        # logger.info(u"title '{}' is too common to match BASE by title".format(my_pub.best_title))
        return

    if my_pub.normalized_titles:
        crossref_title_hit = my_pub.normalized_titles[0]
        for pmh_record_title_obj in crossref_title_hit.matching_pmh_record_title_views:
            # if do_scrape:
            #     pmh_record_obj = db.session.query(PmhRecord).get(pmh_record_title_obj.id)
            #     pmh_record_obj.find_fulltext()
            #     pmh_record_title_obj = pmh_record_obj
            match_type = None
            if my_pub.first_author_lastname or my_pub.last_author_lastname:
                pmh_record_authors = pmh_record_title_obj.authors
                if pmh_record_authors:
                    try:
                        base_doc_author_string = u", ".join(pmh_record_authors)
                        if my_pub.first_author_lastname and normalize(my_pub.first_author_lastname) in normalize(base_doc_author_string):
                            match_type = "title and first author"
                        elif my_pub.last_author_lastname and normalize(my_pub.last_author_lastname) in normalize(base_doc_author_string):
                            match_type = "title and last author"
                        else:
                            if DEBUG_BASE:
                                logger.info(u"author check fails, so skipping this record. Looked for {} and {} in {}".format(
                                    my_pub.first_author_lastname, my_pub.last_author_lastname, base_doc_author_string))
                                logger.info(my_pub.authors)
                            continue
                    except TypeError:
                        pass # couldn't make author string
            if not match_type:
                match_type = "title"
            external_locations += pmh_record_title_obj.make_external_locations(my_pub, match_type, do_scrape=do_scrape)

    external_locations_dict = {}
    for loc in external_locations:
        # do it this way so dois get precedence because they happened first
        if not loc.id+loc.url in external_locations_dict:
            external_locations_dict[loc.id+loc.url] = loc

    my_pub.external_location_matches = external_locations_dict.values()

    # print "my_pub.base_matches", [(m.url, m.scrape_evidence) for m in my_pub.base_matches]

    return my_pub.external_location_matches




# titles_string = remove_punctuation("Authors from the periphery countries choose open access more often (preprint)")
# url_template = u"https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi?func=PerformSearch&query=dctitle:({titles_string})&format=json"
# url = url_template.format(titles_string=titles_string)
# logger.info(u"calling base with {}".format(url))
#
# proxy_url = os.getenv("STATIC_IP_PROXY")
# proxies = {"https": proxy_url}
# r = requests.get(url, proxies=proxies, timeout=6)
# r.json()
# id_string = "{}{}".format(dccollection, dcdoi)

