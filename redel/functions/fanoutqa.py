from typing import Literal

import fanoutqa
from fanoutqa.retrieval import Corpus
from kani import ChatMessage, ai_function

from redel.base_kani import BaseKani
from redel.events import BaseEvent


# custom event
class ArticleRetrieved(BaseEvent):
    type: Literal["article_retrieved"] = "article_retrieved"
    id: str
    article_title: str
    article_pageid: int
    article_revid: int


# ai function
class FanOutQAMixin(BaseKani):
    @property
    def max_search_tokens(self):
        return self.engine.max_context_size // 2

    @ai_function()
    def search(self, query: str):
        """Search Wikipedia for an article with the given title, and get its content. If no such article is found, return similar article names."""
        query = query.strip()
        matches = fanoutqa.wiki_search(query)
        for match in matches:
            if match.title.lower() == query.lower():
                found_article = match
                break
        else:
            similar_searches = "\n".join(f"- {m.title}" for m in matches)
            return (
                "No page with that title exists. Try searching for one of these similar articles"
                f" instead:\n{similar_searches}"
            )

        # found match
        prompt = f"<document>\n<title>{found_article.title}</title>\n{{}}</document>"
        self.app.dispatch(
            ArticleRetrieved(
                id=self.id,
                article_title=found_article.title,
                article_pageid=found_article.pageid,
                article_revid=found_article.revid,
            )
        )

        # if the content fits in the context, return that
        wiki_content = fanoutqa.wiki_content(found_article)
        full_content = prompt.format(f"<content>\n{wiki_content}\n</content>\n")
        if self.message_token_len(ChatMessage.user(full_content)) <= self.max_search_tokens:
            return full_content

        # else, upgrade the engine to long_engine and try again
        self.engine = self.app.long_engine
        if self.message_token_len(ChatMessage.user(full_content)) <= self.max_search_tokens:
            return full_content

        # else, retrieve as many fragments as fit in the context window
        corpus = Corpus([found_article], doc_len=1024)
        user_query = self.last_user_message.text
        retrieved_docs = []
        for doc in corpus.best(user_query):
            formatted = f"<fragment>\n{doc.content}\n</fragment>\n"
            content = prompt.format("".join(retrieved_docs) + formatted)
            doc_len = self.engine.message_len(ChatMessage.user(content))
            if doc_len > self.max_search_tokens:
                break
            retrieved_docs.append(formatted)
        return prompt.format("".join(retrieved_docs))