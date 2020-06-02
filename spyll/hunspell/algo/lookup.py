import re
import itertools
import functools

from enum import Enum
from typing import List, Iterator, Union, Optional

import dataclasses
from dataclasses import dataclass

from pygtrie import CharTrie

from spyll.hunspell import data
import spyll.hunspell.algo.capitalization as cap
import spyll.hunspell.algo.permutations as pmt


CompoundPos = Enum('CompoundPos', 'BEGIN MIDDLE END')


class Affixes(CharTrie):
    def lookup(self, prefix):
        return [val for _, vals in self.prefixes(prefix) for val in vals]


@dataclass
class CompoundRule:
    text: str

    def __post_init__(self):
        # TODO: proper flag parsing! Long is (aa)(bb)*(cc), numeric is (1001)(1002)*(1003)
        self.flags = set(re.sub(r'[\*\?]', '', self.text))
        parts = re.findall(r'[^*?][*?]?', self.text)
        self.re = re.compile(self.text)
        self.partial_re = re.compile(
            functools.reduce(lambda res, part: f"{part}({res})?", parts[::-1])
        )

    def fullmatch(self, flag_sets):
        relevant_flags = [self.flags.intersection(f) for f in flag_sets]
        return any(
            self.re.fullmatch(''.join(fc))
            for fc in itertools.product(*relevant_flags)
        )

    def partial_match(self, flag_sets):
        relevant_flags = [self.flags.intersection(f) for f in flag_sets]
        return any(
            self.partial_re.fullmatch(''.join(fc))
            for fc in itertools.product(*relevant_flags)
        )

@dataclass
class CompoundPattern:
    left: str
    right: str
    replacement: Optional[str] = None

    def __post_init__(self):
        if '/' in self.left:
            self.left_stem, self.left_flag = self.left.split('/')
        else:
            self.left_stem, self.left_flag = self.left, None

        if '/' in self.right:
            self.right_stem, self.right_flag = self.right.split('/')
        else:
            self.right_stem, self.right_flag = self.right, None

    def match(self, left, right):
        # TODO: we don't check flags yet
        return left.stem.endswith(self.left_stem) and right.stem.startswith(self.right_stem)


@dataclass
class WordForm:
    text: str
    stem: str
    prefix: Optional[data.aff.Prefix] = None
    suffix: Optional[data.aff.Suffix] = None
    prefix2: Optional[data.aff.Prefix] = None
    suffix2: Optional[data.aff.Suffix] = None

    def replace(self, **changes):
        return dataclasses.replace(self, **changes)

    def is_base(self):
        return not self.suffix and not self.prefix

    def affix_flags(self):
        flags = self.prefix.flags if self.prefix else set()
        if self.suffix:
            return flags.union(self.suffix.flags)
        else:
            return flags

    def all_affixes(self):
        return list(filter(None, [self.prefix2, self.prefix, self.suffix, self.suffix2]))


Compound = List[WordForm]

class Analyzer:
    def __init__(self, aff: data.aff, dic: data.dic):
        self.aff = aff
        self.dic = dic
        self.compile()

    def compile(self):
        self.suffixes = Affixes()
        self.prefixes = Affixes()

        def suffix_regexp(suffix):
            cond_parts = re.findall(r'(\[.+\]|[^\[])', suffix.condition)
            if suffix.strip:
                cond_parts = cond_parts[:-len(suffix.strip)]

            if cond_parts and cond_parts != ['.']:
                cond = '(?<=' + ''.join(cond_parts) + ')'
            else:
                cond = ''
            return re.compile(cond + suffix.add + '$')

        def prefix_regexp(prefix):
            cond_parts = re.findall(r'(\[.+\]|[^\[])', prefix.condition)
            cond_parts = cond_parts[len(prefix.strip):]

            if cond_parts and cond_parts != ['.']:
                cond = '(?=' + ''.join(cond_parts) + ')'
            else:
                cond = ''
            return re.compile('^' + prefix.add + cond)

        for add, sufs in itertools.groupby(itertools.chain.from_iterable(self.aff.SFX.values()), lambda suf: suf.add[::-1]):
            self.suffixes[add] = [
                (suf, suffix_regexp(suf))
                for suf in sufs
            ]

        for add, prefs in itertools.groupby(itertools.chain.from_iterable(self.aff.PFX.values()), lambda pref: pref.add):
            self.prefixes[add] = [
                (pref, prefix_regexp(pref))
                for pref in prefs
            ]

        self.compoundrules = [CompoundRule(r) for r in self.aff.COMPOUNDRULE]
        self.compoundpatterns = [CompoundPattern(*row) for row in self.aff.CHECKCOMPOUNDPATTERN]

        self.breakpatterns = [
            re.compile(f"({pat})") if pat.startswith('^') or pat.endswith('$') else re.compile(f".({pat}).")
            for pat in self.aff.BREAK
        ]


    def lookup(self, word: str, *, capitalization=True, allow_nosuggest=True) -> bool:
        if self.aff.FORBIDDENWORD and \
           self.dic.homonyms(word) and all(self.aff.FORBIDDENWORD in w.flags for w in self.dic.homonyms(word)):
            return False

        if self.aff.ICONV:
            for (i, o) in sorted(self.aff.ICONV, key=lambda io: len(io[1]), reverse=True):
                word = word.replace(i, o)

        def is_found(variant):
            return any(
                self.analyze(variant, capitalization=capitalization, allow_nosuggest=allow_nosuggest)
            )

        if is_found(word):
            return True

        def try_break(text, depth=0):
            if depth > 10:
                return

            yield [text]
            for pat in self.breakpatterns:
                for m in re.finditer(pat, text):
                    start = text[:m.start(1)]
                    rest = text[m.end(1):]
                    for breaking in try_break(rest, depth=depth+1):
                        yield [start, *breaking]

        for parts in try_break(word):
            if all(is_found(part) for part in parts if part):
                return True

        return False


    def analyze(self, word: str, *,
                capitalization=True,
                allow_nosuggest=True) -> Iterator[Union[WordForm, Compound]]:

        def analyze_internal(variant, captype):
            return itertools.chain(
                self.word_forms(variant, captype=captype, allow_nosuggest=allow_nosuggest),
                self.compound_parts(variant, allow_nosuggest=allow_nosuggest)
            )

        if capitalization:
            captype, variants = cap.variants(word)

            return itertools.chain.from_iterable(
                analyze_internal(v, captype=captype)
                for v in variants
            )
        else:
            return analyze_internal(word)


    def word_forms(
            self,
            word: str,
            captype: cap.Cap = cap.Cap.NO,   # FIXME: Is it so?..
            compoundpos: Optional[CompoundPos] = None,
            allow_nosuggest=True) -> Iterator[WordForm]:

        for form in self.try_affix_forms(word, compoundpos=compoundpos):
            found = False
            # Base (no suffixes) homonym is allowed if exists.
            # And if it would not, we would not be here at all.
            if compoundpos or not form.is_base():
                if any(self.aff.FORBIDDENWORD in dword.flags for dword in self.dic.homonyms(form.stem)):
                    return

            for w in self.dic.homonyms(form.stem):
                if self.have_compatible_flags(w, form, compoundpos=compoundpos,
                                         captype=captype,
                                         allow_nosuggest=allow_nosuggest):
                    found = True
                    yield form

            if not found:
                for w in self.dic.homonyms(form.stem, ignorecase=True):
                    # If the dictionary word is not lowercase, we accept only exactly that
                    # case (above), or ALLCAPS
                    if captype != cap.Cap.ALL and cap.guess(w.stem) != cap.Cap.NO:
                        continue
                    if self.have_compatible_flags(w, form, compoundpos=compoundpos,
                                                  captype=captype,
                                                  allow_nosuggest=allow_nosuggest):
                        yield form


    def compound_parts(self, word: str, allow_nosuggest=True) -> Iterator[Compound]:
        aff = self.aff

        if aff.COMPOUNDBEGIN or aff.COMPOUNDFLAG:
            by_flags = self.compound_parts_by_flags(word, allow_nosuggest=allow_nosuggest)
        else:
            by_flags = iter(())

        if self.compoundrules:
            by_rules = self.compound_parts_by_rules(word, allow_nosuggest=allow_nosuggest)
        else:
            by_rules = iter(())

        def bad_compound(compound):
            for left_paradigm in compound[:-1]:
                left = left_paradigm.text

                if aff.COMPOUNDFORBIDFLAG:
                    # We don't check right: compoundforbid prohibits words at the beginning and middle
                    for dword in self.dic.homonyms(left):
                        if aff.COMPOUNDFORBIDFLAG in dword.flags:
                            return True

                for right_paradigm in compound[1:]:
                    right = right_paradigm.text
                    if aff.CHECKCOMPOUNDREP:
                        for candidate in pmt.replchars(left + right, aff.REP):
                            if isinstance(candidate, str) and any(self.word_forms(candidate)):
                                return True
                    if aff.CHECKCOMPOUNDTRIPLE:
                        if len(set(left[-2:] + right[:1])) == 1 or len(set(left[-1:] + right[:2])) == 1:
                            return True
                    if aff.CHECKCOMPOUNDCASE:
                        r = right[0]
                        l = left[-1]
                        if (r == r.upper() or l == l.upper()) and r != '-' and l != '-':
                            return True
                    if aff.CHECKCOMPOUNDPATTERN:
                        if any(rule.match(left_paradigm, right_paradigm) for rule in self.compoundpatterns):
                            return True
            return False

        yield from (compound
                    for compound in itertools.chain(by_flags, by_rules)
                    if not bad_compound(compound))


    def have_compatible_flags(
            self,
            dictionary_word: data.dic.Word,
            form: WordForm,
            compoundpos: Optional[CompoundPos],
            captype: cap.Cap,
            allow_nosuggest=True) -> bool:

        aff = self.aff

        all_flags = dictionary_word.flags.union(form.affix_flags())

        if not allow_nosuggest and aff.NOSUGGEST in dictionary_word.flags:
            return False

        # Check capitalization
        if aff.KEEPCASE and aff.KEEPCASE in dictionary_word.flags and captype != cap.guess(dictionary_word.stem):
            return False

        # Check affix flags
        # TODO: pseudoroot is an old synonym for needaffix
        if aff.NEEDAFFIX:
            affixes = form.all_affixes()
            if aff.NEEDAFFIX in dictionary_word.flags and not affixes:
                return False
            if affixes and all(aff.NEEDAFFIX in a.flags for a in affixes):
                return False

        if form.prefix and form.prefix.flag not in all_flags:
            return False
        if form.suffix and form.suffix.flag not in all_flags:
            return False

        # Check compound flags

        if not compoundpos:
            return aff.ONLYINCOMPOUND not in all_flags

        if aff.COMPOUNDFLAG in all_flags:
            return True

        if compoundpos == CompoundPos.BEGIN:
            return aff.COMPOUNDBEGIN in all_flags
        elif compoundpos == CompoundPos.END:
            return aff.COMPOUNDLAST in all_flags
        elif compoundpos == CompoundPos.MIDDLE:
            return aff.COMPOUNDMIDDLE in all_flags
        else:
            # shoulnd't happen
            return False


    # Affixes-related algorithms
    # --------------------------


    def try_affix_forms(
            self,
            word: str,
            compoundpos: Optional[CompoundPos] = None) -> Iterator[WordForm]:

        yield WordForm(word, word)    # "Whole word" is always existing option

        aff = self.aff

        if compoundpos:
            suffix_allowed = compoundpos == CompoundPos.END or aff.COMPOUNDPERMITFLAG
            prefix_allowed = compoundpos == CompoundPos.BEGIN or aff.COMPOUNDPERMITFLAG
            prefix_required_flags = [] if compoundpos == CompoundPos.BEGIN else [aff.COMPOUNDPERMITFLAG]
            suffix_required_flags = [] if compoundpos == CompoundPos.END else [aff.COMPOUNDPERMITFLAG]
            forbidden_flags = [aff.COMPOUNDFORBIDFLAG] if aff.COMPOUNDFORBIDFLAG else []
        else:
            suffix_allowed = True
            prefix_allowed = True
            prefix_required_flags = []
            suffix_required_flags = []
            forbidden_flags = []

        if suffix_allowed:
            yield from self.desuffix(word, required_flags=suffix_required_flags, forbidden_flags=forbidden_flags)

        if prefix_allowed:
            for form in self.deprefix(word, required_flags=prefix_required_flags, forbidden_flags=forbidden_flags):
                yield form

                if suffix_allowed and form.prefix.crossproduct:
                    yield from (
                        form2.replace(prefix=form.prefix)
                        for form2 in self.desuffix(form.stem,
                                              required_flags=suffix_required_flags,
                                              forbidden_flags=forbidden_flags,
                                              crossproduct=True)
                    )


    def desuffix(
            self,
            word: str,
            required_flags: List[str] = [],
            forbidden_flags: List[str] = [],
            nested: bool = False,
            crossproduct: bool = False) -> Iterator[WordForm]:

        def good_suffix(suffix):
            return (not crossproduct or suffix.crossproduct) and \
                    all(f in suffix.flags for f in required_flags) and \
                    all(f not in suffix.flags for f in forbidden_flags)

        possible_suffixes = (
            (suffix, regexp)
            for suffix, regexp in self.suffixes.lookup(word[::-1])
            if good_suffix(suffix) and regexp.search(word)
        )

        for suffix, regexp in possible_suffixes:
            stem = regexp.sub(suffix.strip, word)

            yield WordForm(word, stem, suffix=suffix)

            if not nested:  # only one level depth
                for form2 in self.desuffix(stem,
                                      required_flags=[suffix.flag, *required_flags],
                                      forbidden_flags=forbidden_flags,
                                      nested=True,
                                      crossproduct=crossproduct):
                    yield form2.replace(suffix2=suffix, text=word)


    def deprefix(
            self,
            word: str,
            required_flags: List[str] = [],
            forbidden_flags: List[str] = [],
            nested: bool = False) -> Iterator[WordForm]:

        def good_prefix(prefix):
            return all(f in prefix.flags for f in required_flags) and \
                   all(f not in prefix.flags for f in forbidden_flags)

        possible_prefixes = (
            (prefix, regexp)
            for prefix, regexp in self.prefixes.lookup(word)
            if good_prefix(prefix) and regexp.search(word)
        )

        for prefix, regexp in possible_prefixes:
            stem = regexp.sub(prefix.strip, word)

            yield WordForm(word, stem, prefix=prefix)

            # TODO: Only if compoundpreffixes are allowed in *.aff
            if not nested:  # only one level depth
                for form2 in self.deprefix(stem,
                                      required_flags=[prefix.flag, *required_flags],
                                      forbidden_flags=forbidden_flags,
                                      nested=True):
                    yield form2.replace(prefix2=prefix, text=word)


    # Compounding details
    # -------------------


    def compound_parts_by_flags(
            self,
            word_rest: str,
            prev_parts: List[WordForm] = [],
            allow_nosuggest=True) -> Iterator[List[WordForm]]:

        aff = self.aff

        # If it is middle of compounding process "the rest of the word is the whole last part" is always
        # possible
        if prev_parts:
            for form in self.word_forms(word_rest,
                                            compoundpos=CompoundPos.END,
                                            allow_nosuggest=allow_nosuggest):
                yield [form]

        if len(word_rest) < aff.COMPOUNDMIN * 2 or \
                (aff.COMPOUNDWORDSMAX and len(prev_parts) >= aff.COMPOUNDWORDSMAX):
            return

        compoundpos = CompoundPos.BEGIN if not prev_parts else CompoundPos.MIDDLE

        for pos in range(aff.COMPOUNDMIN, len(word_rest) - aff.COMPOUNDMIN + 1):
            beg = word_rest[0:pos]

            for form in self.word_forms(beg, compoundpos=compoundpos,
                                            allow_nosuggest=allow_nosuggest):
                parts = [*prev_parts, form]
                for rest in self.compound_parts_by_flags(word_rest[pos:], parts,
                                                    allow_nosuggest=allow_nosuggest):
                    yield [form, *rest]


    def compound_parts_by_rules(
            self,
            word_rest: str,
            prev_parts: List[data.dic.Word] = [],
            rules: Optional[List[CompoundRule]] = None,
            allow_nosuggest=True) -> Iterator[List[WordForm]]:

        aff = self.aff
        if rules is None:
            rules = self.compoundrules

        # FIXME: ignores flags like FORBIDDENWORD and nosuggest

        # If it is middle of compounding process "the rest of the word is the whole last part" is always
        # possible
        if prev_parts:
            for homonym in self.dic.homonyms(word_rest):
                parts = [*prev_parts, homonym]
                flag_sets = [w.flags for w in parts]
                if any(r.fullmatch(flag_sets) for r in rules):
                    yield [WordForm(word_rest, word_rest)]

        if len(word_rest) < aff.COMPOUNDMIN * 2 or \
                (aff.COMPOUNDWORDSMAX and len(prev_parts) >= aff.COMPOUNDWORDSMAX):
            return

        for pos in range(aff.COMPOUNDMIN, len(word_rest) - aff.COMPOUNDMIN + 1):
            beg = word_rest[0:pos]
            for homonym in self.dic.homonyms(beg):
                parts = [*prev_parts, homonym]
                flag_sets = [w.flags for w in parts]
                compoundrules = [r for r in rules if r.partial_match(flag_sets)]
                if compoundrules:
                    by_rules = self.compound_parts_by_rules(word_rest[pos:], rules=rules, prev_parts=parts)
                    for rest in by_rules:
                        yield [WordForm(beg, beg), *rest]
