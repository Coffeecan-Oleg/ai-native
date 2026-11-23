"""Lexical retrieval over a caller-supplied, already authorised collection."""
import re


WORDS = re.compile(r"[^\W_]+", re.UNICODE)
STOP = set("""а без бы в во где дай для до есть же за и из или к как какая какие
какой какую каковы ли мне мой мы на нам нас найти найди нужен нужно о об от
по под подбери пожалуйста покажи про с со такая то у что чем это этот я
наш наша наше нашей наши нашу наших называется расскажи подскажи меня хочу можно
помоги чтобы""".split())
CONTEXT = {"компан", "организац", "сотрудник", "навык"}
ENDINGS = re.compile(r"(иями|ями|ами|иях|ией|ием|ого|его|ому|ему|ыми|ими|ах|ях|ов|ам|ом|ем|"
                     r"ия|ие|ию|ии|ий|ая|яя|ую|юю|ые|ой|ый|ым|ей|а|я|ы|и|у|е|ю|о)$")
GROUPS = (
    ("проверка", "проверь", "проверить", "проверки", "сравни", "сравнить", "сверь", "сверить"),
    ("инструкция", "инструкцию", "инструкции", "руководство", "руководства"),
    ("подготовка", "подготовь", "подготовить", "составь", "составить"),
)
FORMS = {word: group[0] for group in GROUPS for word in group}
REQUISITES = {"инн", "кпп", "огрн"}


def term(word):
    if word in FORMS:
        return FORMS[word]
    if len(word) >= 5 and re.fullmatch("[а-я]+", word):
        return ENDINGS.sub("", word, count=1)
    return word


class SearchQuery:
    def __init__(self, text):
        self.text = text.strip().casefold().replace("ё", "е")
        meaningful = {term(word) for word in WORDS.findall(self.text) if word not in STOP}
        self.terms = meaningful - CONTEXT or meaningful
        self.required = {key for key in self.terms if key.isdigit() or key == "акметрон" or key in REQUISITES}
        self.identifier = bool(re.fullmatch(r"[a-z0-9]+(?:[-_./:][a-z0-9]+)+", self.text))
        self.exact = re.compile(r"(?<!\w)" + re.escape(self.text) + r"(?!\w)")

    def match(self, title, body):
        """Return a sortable score and a word from the original text for the snippet."""
        if not self.text:
            return 1, ""
        if not self.terms:
            return 0, ""
        folded_title, folded_body = title.casefold(), body.casefold()
        title_text = folded_title.replace("ё", "е")
        body_text = folded_body.replace("ё", "е")
        exact_title = self.exact.search(title_text)
        exact_body = self.exact.search(body_text)
        if exact_title or exact_body:
            # Return an original spelling so snippets also find words containing ё.
            original = folded_body if exact_body else folded_title
            start, end = (exact_body or exact_title).span()
            return (200 if exact_title else 100), original[start:end]
        if self.identifier:
            return 0, ""
        matched = {}
        in_title = set()
        for text, heading in ((body, False), (title, True)):
            for match in WORDS.finditer(text):
                word = match.group().casefold()
                key = term(word.replace("ё", "е"))
                if key not in self.terms:
                    # Expand the generic request only towards identifiers actually present.
                    if key in REQUISITES and "реквизит" in self.terms:
                        key = "реквизит"
                    else:
                        continue
                matched.setdefault(key, word)
                if heading:
                    in_title.add(key)
        if len(matched) * 3 < len(self.terms) * 2:
            # Keep short literal searches for word fragments useful, below whole-word hits.
            if len(self.text) >= 4 and self.text.isalpha() and not self.required:
                for normalized, original in ((body_text, folded_body), (title_text, folded_title)):
                    offset = normalized.find(self.text)
                    if offset >= 0:
                        return 1, original[offset:offset + len(self.text)]
            return 0, ""
        if not self.required.issubset(matched):
            return 0, ""
        anchor = next((word for key, word in matched.items() if key != "акметрон"), "")
        if not anchor:
            anchor = next(iter(matched.values()))
        return 10 * len(matched) + 4 * len(in_title), anchor
