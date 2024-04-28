"""
3-Agent system to first extract a few pieces of info, then chat with user.

- Assistant: helps user answer questions about a Book. But first it needs to
    extract some information from a document about the Book, using Extractor.
- Extractor: generates questions about the Book document, one by one,
    then returns all info to Assistant using a tool message.
- DocAgent: answers the questions generated by Extractor, based on the Book doc.

Run like this:

python3 examples/chainlit/extract-then-chat.py

"""

from langroid import ChatDocument
from pydantic import BaseModel
from typing import List
import os
from fire import Fire

from rich import print
import langroid as lr
import langroid.language_models as lm
from langroid.mytypes import Entity
from langroid.agent.special.doc_chat_agent import DocChatAgent, DocChatAgentConfig
from langroid.parsing.parser import ParsingConfig
from langroid.agent.chat_agent import ChatAgent, ChatAgentConfig
from langroid.agent.task import Task
from langroid.agent.tool_message import ToolMessage
from langroid.utils.configuration import set_global, Settings
from langroid.utils.constants import NO_ANSWER, DONE, SEND_TO, PASS

from dotenv import load_dotenv

os.environ["TOKENIZERS_PARALLELISM"] = "false"


class BookInfo(BaseModel):
    title: str
    author: str
    year: int


class BookInfoTool(ToolMessage):
    request: str = "book_info"
    purpose: str = "Collect <info> about Books"

    info: List[BookInfo]

    def handle(self) -> str:
        """Exit task and pass tool to parent"""
        return DONE + " " + PASS

    @classmethod
    def examples(cls) -> List["BookInfoTool"]:
        return [
            cls(
                info=[
                    BookInfo(title="The Hobbit", author="J.R.R. Tolkien", year=1937),
                    BookInfo(
                        title="The Great Gatsby",
                        author="F. Scott Fitzgerald",
                        year=1925,
                    ),
                ]
            )
        ]


class Assistant(ChatAgent):
    def book_info(self, msg: BookInfoTool) -> str:
        # convert info  to NON-JSON so it doesn't look like a tool,
        # and insert routing so that the Assistan't LLM responds to it, not user.
        info_str = str(msg.info).replace("{", "[").replace("}", "]")
        return f"""{SEND_TO}LLM
        Below is INFO about various books, you received from the Extractor.
        Now ask the user what help they need, and respond ONLY based on this INFO.
        
        INFO: 
        {info_str} 
        """


class Extractor(ChatAgent):
    def handle_message_fallback(
        self, msg: str | ChatDocument
    ) -> str | ChatDocument | None:
        """Nudge LLM when it fails to use book_info correctly"""
        if self.has_tool_message_attempt(msg):
            return """
            You must use the "book_info" tool to present the info.
            You either forgot to use it, or you used it with the wrong format.
            Make sure all fields are filled out and pay attention to the 
            required types of the fields.
            """


def chat(
    model: str = "",  # or, e.g., "ollma/mistral:7b-instruct-v0.2-q8_0"
    debug: bool = False,
    no_cache: bool = False,  # whether to disablue using cached LLM responses
):
    print(
        """
        Hello! I am your book info helper. 
        First I will get info about some books
        """
    )

    load_dotenv()

    set_global(
        Settings(
            debug=debug,
            cache=not no_cache,  # disables cache lookup; set to True to use cache
        )
    )

    llm_cfg = lm.OpenAIGPTConfig(
        # or, e.g. "ollama/mistral:7b-instruct-v0.2-q8_0" but result may be brittle
        chat_model=model or lm.OpenAIChatModel.GPT4_TURBO,
        chat_context_length=16_000,  # adjust based on model
    )
    doc_agent = DocChatAgent(
        DocChatAgentConfig(
            llm=llm_cfg,
            n_neighbor_chunks=2,
            parsing=ParsingConfig(
                chunk_size=50,
                overlap=10,
                n_similar_docs=3,
                n_neighbor_ids=4,
            ),
            vecdb=lr.vector_store.LanceDBConfig(
                collection_name="book_info",
                replace_collection=True,
                storage_path=".lancedb/data/",
                embedding=lr.embedding_models.SentenceTransformerEmbeddingsConfig(
                    model_type="sentence-transformer",
                    model_name="BAAI/bge-large-en-v1.5",
                ),
            ),
            cross_encoder_reranking_model="",
        )
    )
    doc_agent.ingest_doc_paths(["examples/docqa/books.txt"])
    doc_task = Task(
        doc_agent,
        name="DocAgent",
        done_if_no_response=[Entity.LLM],  # done if null response from LLM
        done_if_response=[Entity.LLM],  # done if non-null response from LLM
        # Don't use system_message here since it will override doc chat agent's
        # default system message
    )

    extractor_agent = Extractor(
        ChatAgentConfig(
            llm=llm_cfg,
            vecdb=None,
        )
    )
    extractor_agent.enable_message(BookInfoTool)

    extractor_task = Task(
        extractor_agent,
        name="Extractor",
        interactive=False,  # set to True to slow it down (hit enter to progress)
        system_message=f"""
        You are an expert at understanding JSON function/tool specifications.
        You must extract information about various books from a document,
        to finally present the info using the `book_info` tool/function,
        but you do not have access to the document. 
        I can help with your questions about the document.
        You have to ask questions in these steps:
        1. ask which books are in the document
        2. for each book, ask the various pieces of info you need.
        
        If I am unable to answer your question initially, try asking differently,
        and if I am still unable to answer after 3 tries, 
        fill in {NO_ANSWER} for that field. 
        Think step by step. 
        
        Do not explain yourself, or say any extraneous things. 
        When you receive the answer, then ask for the next field, and so on.
        """,
    )

    assistant_agent = Assistant(
        ChatAgentConfig(
            llm=llm_cfg,
            vecdb=None,
        )
    )
    assistant_agent.enable_message(lr.agent.tools.RecipientTool)
    # enable assistant to HANDLE the book_info tool but not USE it
    assistant_agent.enable_message(BookInfoTool, use=False, handle=True)
    assistant_task = Task(
        assistant_agent,
        name="Assistant",
        interactive=True,
        system_message="""
        You are a helpful librarian, answering my (the user) questions about 
        books described in a certain document, and you do NOT know which 
        books are in the document.
        
        FIRST you need to ask the "Extractor" to collect information
        about various books that are in a certain document. Address your request to the 
        Extractor using the 'recipient_message' tool/function. 
        
        Once you receive the information, you should then ask me (the user) 
        what I need help with.                
        """,
    )

    assistant_task.add_sub_task([extractor_task])
    extractor_task.add_sub_task([doc_task])

    # must use run() instead of run_async() because DocChatAgent
    # does not have an async llm_response method
    assistant_task.run()


if __name__ == "__main__":
    Fire(chat)