#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Usage:
  engineering-to-tt [options] <ical_file>...

Options:
  -t=<tripos_name>    Set the tripos name to use in the output.
                      Default: engineering
  -s=<file>           Specify a file to load substitutions from
  -e=<file>           Specify a file to load exclusions from

Substitutions:
  The names used in the iCalendar feed are not necessarily what we wish to
  output in the Timetable XML. By specifying a substitution file these names
  can be mapped to more familiar equivalents. For example, the parts 1, 2, 3
  and 4 can be mapped to IA, IB, IIA, and IIB respectively.

  The file should contain a JSON document with the following structure:

    {
        "substitutions": {
            "__all__": {
                "parts": {
                    "1": "IA",
                    "2": "IB",
                    "3": "IIA",
                    "4": "IIB"
                },
                "event_types": {
                    "C": "class",
                    "L": "lecture"
                }
            },
            "1": {
                "papers": {
                    "CW": "Coursework",
                    "P1": "Paper 1 — Mechanical engineering"
                }
            },
            "2": {
                "papers": {
                    "CW": "Coursework",
                    "P1": "Paper 1 — Mechanics",
                    "P2": "Paper 2 — Structures"
                }
            },
            "3": {},
            "4": {}
        }
    }

  All the values in the "substitutions" object share the same format.

  Substitutions work as follows. When reading a part, paper or type name from
  an iCalendar event, the substitutions lookup table of the associated type
  will be checked for a match. If a match in the table is found, the value from
  the table is used instead of the value from the iCalendar event. For example,
  when reading the part "1", a match is found in the "__all__" lookup table,
  and the value "IA" is used instead of "1".

  Substitutions specific to a certain part can be specified by placing the
  substitutions under the part's name. Global substitutions go in the "__all__"
  section.

Exclusions:

  When an event is generated we check whether its values correspond to any of
  the exclusions specified. If it does it won't be included in the generated
  XML. Exclusions are specified through a json file by specifying a list of
  objects with values with which events need to correspond in order to be
  exluded.

  Example structure:

    {
        "exclusions": [
            {
                "paper": "Coursework",
                "event_type": "class"
            }
        ]
    }

"""
from __future__ import unicode_literals

import copy
import hashlib
import itertools
import itertools
import json
import os
import re
import sys
import urllib
import urlparse

from lxml import etree
from requests.exceptions import RequestException
import docopt
import icalendar
import pytz
import requests

from ttapiutils.utils import write_c14n_pretty
from ttapiutils.autoimport import DataSourceParamsException


__version__ = "0.1.2"
__version_info__ = tuple(int(i) for i in __version__.split("."))


# Don't think this is going to change any time soon
TIMETABLE_TIMEZONE = pytz.timezone("Europe/London")

# Some random bytes to seed our ID generation with
EXTERNAL_ID_SEED = b'\x07\xb8\xb7\xbc\xf1\xf1\xfc\x06;\xc2\x1bC<,\x14L'

ENGINEERING_ICAL_URL = "http://td.eng.cam.ac.uk/tod/public/view_ical.php"

DEFAULT_TRIPOS_NAME = "engineering"

DEFAULT_TERMS = tuple("MLE")


class EngineeringToTTException(Exception):
    pass


class ICalSourceException(EngineeringToTTException):
    pass


class SubstitutionFormatException(EngineeringToTTException):
    pass


class ExcluderFormatException(EngineeringToTTException):
    pass


class ParseException(EngineeringToTTException):
    pass


class EventParseException(ParseException):
    pass


class Substitutor(object):
    def __init__(self, substitutions):
        self.all = substitutions.get("__all__", {})
        self.substitutions = substitutions

    def lookup(self, section, value_type, value):
        if section in self.substitutions:
            if value_type in self.substitutions[section]:
                if value in self.substitutions[section][value_type]:
                    return self.substitutions[section][value_type][value]

        if section != "__all__":
            return self.lookup("__all__", value_type, value)
        return value

    @staticmethod
    def from_json_file(filename):
        try:
            with open(filename) as f:
                return Substitutor.from_json(json.load(f))
        except ValueError as e:
            raise SubstitutionFormatException(
                "Unable to parse substitution file as JSON: {}".format(e))

    @staticmethod
    def from_json(json_object):
        if not isinstance(json_object, dict):
            raise SubstitutionFormatException(
                "Top level value was not an object.")
        if "substitutions" not in json_object:
            raise SubstitutionFormatException(
                "Top level object has no \"substitutions\" key.")
        # TODO: validate more meticulously if good error handling/reporting is
        # desired.

        return Substitutor(json_object["substitutions"])

    def as_json(self):
        return {
            "substitutions": copy.deepcopy(self.substitutions)
        }


class Excludor(object):
    def __init__(self, exclusions):
        self.exclusions = exclusions

    def is_excluded(self, event):
        for exclusion in self.exclusions:
            if all(
                getattr(event, exclusion_key, None)
                    == exclusion[exclusion_key]
                    for exclusion_key in exclusion):
                return True
        return False

    @staticmethod
    def from_json_file(filename):
        try:
            with open(filename) as f:
                return Excludor.from_json(json.load(f))
        except ValueError as e:
            raise ExcluderFormatException(
                "Unable to parse substitution file as JSON: {}".format(e))

    @staticmethod
    def from_json(json_object):
        if not isinstance(json_object, dict):
            raise ExcluderFormatException(
                "Top level value was not an object.")
        if "exclusions" not in json_object:
            raise ExcluderFormatException(
                "Top level object has no \"exclusions\" key.")

        return Excludor(json_object["exclusions"])

    def as_json(self):
        return {
            "exclusions": copy.deepcopy(self.exclusions)
        }


class NullExcludor(object):
    def __init__(self):
        pass

    def is_excluded(self, event):
        return False


class NullSubstitutor(object):
    def __init__(self):
        pass

    def lookup(self, section, value_type, value):
        return value


DEFAULT_EXCLUDOR = NullExcludor()
DEFAULT_SUBSTITUTOR = NullSubstitutor()


def parse_engineering_ical_string(ical_string, substitutor):
    calendar = icalendar.Calendar.from_ical(ical_string)

    return [parse_engineering_event(e, substitutor)
            for e in calendar.subcomponents
            if isinstance(e, icalendar.Event)]


def parse_engineering_event(ical_event, substitutor):
    return (EngineeringEvent.from_ical_event(ical_event)
            .with_substitutions(substitutor))


class EngineeringEvent(object):
    ICAL_SUMMARY_PATTERN = re.compile(
        r"""^(\d)([A-Z0-9]+)/(.+)\[(\d+)\]([A-Z+]) (.*)\((.*)\)$""")

    def __init__(self, part, paper, name, event_type, staff_name, location,
                 start, end, uid):
        self.part = part
        self.paper = paper
        self.name = name
        self.event_type = event_type
        self.staff_name = staff_name
        self.location = location
        self.start = start
        self.end = end
        self.uid = uid

        assert self.start.tzinfo.zone == TIMETABLE_TIMEZONE.zone
        assert self.end.tzinfo.zone == TIMETABLE_TIMEZONE.zone

    def with_substitutions(self, substitutor):
        part = self.part
        return EngineeringEvent(
            substitutor.lookup(part, "parts", part),
            substitutor.lookup(part, "papers", self.paper),
            self.name,
            substitutor.lookup(part, "event_types", self.event_type),
            self.staff_name,
            self.location,
            self.start,
            self.end,
            self.uid
        )

    def _as_tuple(self):
        """Get a tuple of the event's fields"""
        return (
            self.part, self.paper, self.name, self.event_type, self.staff_name,
            self.location, self.start, self.end, self.uid
        )

    def __hash__(self):
        return hash(self._as_tuple())

    def __eq__(self, other):
        return isinstance(other, EngineeringEvent) and (
               self._as_tuple() == other._as_tuple())

    @staticmethod
    def from_ical_event(ical_event):
        if "SUMMARY" not in ical_event:
            raise EventParseException("Event has no SUMMARY", ical_event)

        summary = ical_event["SUMMARY"]
        match = EngineeringEvent.ICAL_SUMMARY_PATTERN.match(summary)
        if not match:
            raise EventParseException(
                "SUMMARY did not match expected format: {!r}".format(summary),
                ical_event)

        if "DTSTART" not in ical_event:
            raise EventParseException("Event has no DTSTART", ical_event)
        if "DTEND" not in ical_event:
            raise EventParseException("Event has no DTEND", ical_event)

        return EngineeringEvent(
            match.group(1),  # part
            match.group(2),  # paper code
            match.group(3),  # event/series name
            match.group(5),  # event type (skip 4 which is term week)
            match.group(6),  # staff (e.g. lecturer) name
            match.group(7),  # location/room name

            # The API expects times to be in localtime (to Europe/London)
            ical_event["DTSTART"].dt.astimezone(TIMETABLE_TIMEZONE),
            ical_event["DTEND"].dt.astimezone(TIMETABLE_TIMEZONE),
            ical_event["UID"]
        )


def event_sort_key(event):
    assert isinstance(event, EngineeringEvent), event
    return (event.part, event.paper, event.name, event.start)


def build_timetable_xml(events, tripos_name=DEFAULT_TRIPOS_NAME):
    # Order the events to facilitate grouping
    events = sorted(events, key=event_sort_key)

    assert events

    root = etree.Element("moduleList")

    events_by_part = itertools.groupby(events, lambda event: event.part)

    root.extend(
        module
        for (part, events) in events_by_part
        for module in build_part_xml(tripos_name, part, events)
    )
    return root


def build_part_xml(tripos, part, events):
    # Build a module for each paper
    events_by_paper = itertools.groupby(events, lambda event: event.paper)

    return (
        build_paper_xml(tripos, part, paper, events)
        for (paper, events) in events_by_paper
    )


def build_paper_xml(tripos, part, paper, events):
    module = etree.Element("module")
    path = etree.SubElement(module, "path")
    etree.SubElement(path, "tripos").text = tripos
    etree.SubElement(path, "part").text = part
    etree.SubElement(module, "name").text = paper

    events_by_series = itertools.groupby(events, lambda event: event.name)

    module.extend(
        build_series_xml(tripos, part, paper, series, events)
        for (series, events) in events_by_series
    )
    return module


def build_series_xml(tripos, part, paper, series, events):
    series_el = etree.Element("series")
    etree.SubElement(series_el, "uniqueid").text = external_id(
        tripos, part, paper, series)
    etree.SubElement(series_el, "name").text = series

    series_el.extend(
        build_event_xml(event)
        for event in events
    )
    return series_el


def build_event_xml(event):
    event_el = etree.Element("event")
    etree.SubElement(event_el, "uniqueid").text = event.uid
    etree.SubElement(event_el, "name").text = event.name
    etree.SubElement(event_el, "location").text = event.location
    etree.SubElement(event_el, "lecturer").text = event.staff_name

    assert event.start.date() == event.end.date(), (
        "The API assumes events start and finish on the same day")
    etree.SubElement(event_el, "date").text = event.start.strftime("%Y-%m-%d")
    etree.SubElement(event_el, "start").text = event.start.strftime("%H:%M:%S")
    etree.SubElement(event_el, "end").text = event.end.strftime("%H:%M:%S")

    etree.SubElement(event_el, "type").text = event.event_type

    return event_el


def external_id(tripos, part, paper, series):
    # Doesn't really matter which algo we use, there are no security
    # implications.
    h = hashlib.md5()
    h.update(EXTERNAL_ID_SEED)
    h.update((tripos + part + paper + series).encode("utf-8"))
    return h.hexdigest()


def get_substitutor(substitutions_file, cls=Substitutor):
    if substitutions_file:
        return cls.from_json_file(substitutions_file)
    else:
        return DEFAULT_SUBSTITUTOR


def get_excludor(exclusions_file, cls=Excludor):
    if exclusions_file:
        return cls.from_json_file(exclusions_file)
    else:
        return DEFAULT_EXCLUDOR


class HttpICalSource(object):
    def __init__(self, base_url=ENGINEERING_ICAL_URL):
        self.base_url = urlparse.urlparse(base_url)

    def get_year_param(self, year):
        """
        Get a year range of the form 2014_15.
        """
        return "{:d}_{}".format(year, str(year + 1)[-2:])

    def get_url_query_params(self, year, course, term):
        return urllib.urlencode({
            "yearval": self.get_year_param(year),
            "term": term,
            "course": course
        })

    def get_url(self, year, course, term):
        pieces = list(self.base_url)
        pieces[4] = self.get_url_query_params(year, course, term)
        return urlparse.urlunparse(pieces)

    def get_ical(self, fetch_spec):
        url = self.get_url(*fetch_spec)

        try:
            response = requests.get(url, allow_redirects=False)

            if response.status_code != requests.codes.ok:
                raise ICalSourceException(
                    "Non-200 status code received for GET to {}: {}"
                    .format(url, response.status_code))
        except requests.RequestException as e:
            raise ICalSourceException(
                "Error making HTTP request to {}".format(url), e)
        return response.text


class FilesystemICalSource(object):
    def __init__(self, encoding="utf-8"):
        self.encoding = encoding

    def get_ical(self, fetch_spec):
        try:
            with open(fetch_spec) as f:
                return f.read().decode(self.encoding)
        except IOError as e:
            raise ICalSourceException(
                "Unable to read file: {}".format(fetch_spec), e)


def parse_events(ical_source, fetch_specs, substitutor=DEFAULT_SUBSTITUTOR,
                 excludor=DEFAULT_EXCLUDOR):
    return set(
        event
        for fetch_spec in fetch_specs
        for event in parse_engineering_ical_string(
            ical_source.get_ical(fetch_spec), substitutor)
        if not excludor.is_excluded(event)
    )


def engineering_to_timetable_xml(ical_source, fetch_specs,
                                 substitutor=DEFAULT_SUBSTITUTOR,
                                 excludor=DEFAULT_EXCLUDOR,
                                 tripos_name=DEFAULT_TRIPOS_NAME):
    events = parse_events(ical_source, fetch_specs, substitutor, excludor)
    return build_timetable_xml(events, tripos_name=tripos_name)


def main():
    args = docopt.docopt(__doc__)

    tripos_name = args["-t"] or DEFAULT_TRIPOS_NAME

    substitutions_file = args["-s"]
    substitutor = get_substitutor(substitutions_file)

    exclusions_file = args["-e"]
    excludor = get_excludor(exclusions_file)

    ical_source = FilesystemICalSource()
    # fetch specs are just file paths for this source
    fetch_specs = args["<ical_file>"]

    api_xml = engineering_to_timetable_xml(ical_source, fetch_specs,
        substitutor=substitutor, excludor=excludor, tripos_name=tripos_name)

    # Write the XML's bytes to stdout
    # We must write to stdout.buffer in Py3 but just stdout in Py2
    out_file = getattr(sys.stdout, "buffer", sys.stdout)
    write_c14n_pretty(api_xml, out_file)


def get_single_value(params, *args):
    if len(args) not in [1, 2]:
        raise TypeError(
            "2 or 3 arguments expected ({} given)".format(len(args) + 1))
    try:
        name = args[0]
        value = params[name]
    except KeyError:
        if len(args) == 2:
            return args[1]
        raise
    if isinstance(value, list):
        if len(value) > 1:
            raise DataSourceParamsException(
                "Single value expected for {!r}, got {}: {}"
                .format(name, len(value), value))
        return value[0]
    return value


def get_list_value(params, name):
    value = params.get(name, [])
    if not isinstance(value, list):
        return [value]
    return value


def data_source_factory(params):
    tripos = get_single_value(params, "tripos", DEFAULT_TRIPOS_NAME)
    sub_file = get_single_value(params, "substitutions", None)
    exclusions_file = get_single_value(params, "exclusions", None)
    audit_log = get_single_value(params, "audit_log", None)

    year_val = get_single_value(params, "year", None)
    if year_val is None:
        raise DataSourceParamsException("A year is required.")
    try:
        year = int(year_val)
    except:
        raise DataSourceParamsException(
            "Unable to interpret year as an int: {}".format(year_val))

    parts = get_list_value(params, "part")
    if not parts:
        raise DataSourceParamsException("At least one part is required.")

    args = [tripos, year, parts, sub_file, exclusions_file]
    if audit_log is None:
        return EngineeringDataSource(*args)
    args = [audit_log] + args
    return AuditedEngineeringDataSource(*args)


class EngineeringDataSource(object):
    def __init__(self, tripos, year, parts, substitutions_file,
        exclusions_file, terms=DEFAULT_TERMS):
        self._tripos = tripos
        self._year = year
        self._parts = parts
        self._terms = terms

        self._substitutions_file = substitutions_file
        self._exclusions_file = exclusions_file

        self._substitutor = get_substitutor(substitutions_file)
        self._excludor = get_excludor(exclusions_file)

    def get_tripos_name(self):
        return self._tripos

    def get_substitutor(self):
        return self._substitutor

    def get_excludor(self):
        return self._excludor

    def get_ical_source(self):
        return HttpICalSource()

    def get_fetch_specs(self):
        """
        Get a sequence of (year, part, term) tuples to fetch from the
        Engineering teaching database.
        """
        return itertools.product((self._year,), self._parts, self._terms)

    def get_xml(self):
        return engineering_to_timetable_xml(
            self.get_ical_source(), self.get_fetch_specs(),
            substitutor=self.get_substitutor(), excludor=self.get_excludor(),
            tripos_name=self.get_tripos_name())


class AuditedHttpICalSource(HttpICalSource):
    def __init__(self, audit_log, **kwargs):
        super(AuditedHttpICalSource, self).__init__(**kwargs)
        self.audit_log = audit_log

    def get_audit_filename(self, year, course, term):
        return "engineering-{}-{}-{}.ics".format(year, course, term)

    def get_ical(self, fetch_spec):
        ical = super(AuditedHttpICalSource, self).get_ical(fetch_spec)

        with self.audit_log.open_audit_file(
            self.get_audit_filename(*fetch_spec)) as f:
            f.write(ical)
        return ical


class AuditedEngineeringDataSource(EngineeringDataSource):
    def __init__(self, audit_log, *args):
        super(AuditedEngineeringDataSource, self).__init__(*args)
        self.audit_log = audit_log

    def get_ical_source(self):
        return AuditedHttpICalSource(self.audit_log)

    def log_manifest(self):
        manifest = {
            "version": __version__,
            "tripos_name": self.get_tripos_name(),
            "exclusions_file": self._exclusions_file,
            "substitutions_file": self._substitutions_file,
            "year": self._year,
            "parts": self._parts,
            "terms": self._terms
        }
        self.audit_log.log_json("engineering-manifest", manifest)

    def log_substitutor(self):
        substitutor = self.get_substitutor()
        if isinstance(substitutor, Substitutor):
            with self.audit_log.open_audit_file(
                "engineering-substitutions.json") as f:
                json.dump(substitutor.as_json(), f, indent=4)

    def log_excludor(self):
        excludor = self.get_excludor()
        if isinstance(excludor, Excludor):
            with self.audit_log.open_audit_file(
                "engineering-exclusions.json") as f:
                json.dump(excludor.as_json(), f, indent=4)

    def get_xml(self):
        # Log our settings before sending the XML
        self.log_manifest()
        self.log_substitutor()
        self.log_excludor()

        return super(AuditedEngineeringDataSource, self).get_xml()


if __name__ == "__main__":
    main()
