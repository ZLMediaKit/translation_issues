# -*- coding:utf-8 -*-
#  Copyright (c) 2016-present The ZLMediaKit project authors. All Rights Reserved.
#  This file is part of ZLMediaKit(https://github.com/ZLMediaKit/Github-AI-Assistant).
#  Use of this source code is governed by MIT-like license that can be found in the
#  LICENSE file in the root of the source tree. All contributing project authors
#  may be found in the AUTHORS file in the root of the source tree.
#
"""
@author:alex
@date:2024/9/15
@time:上午3:02
"""
__author__ = 'alex'

import asyncio
import glob
import json
import os
import re
import shutil
from typing import List, Dict, Any, Optional

import git
from pymilvus import DataType, MilvusClient, FieldSchema, CollectionSchema, MilvusException
from pymilvus.milvus_client.index import IndexParams
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from core import settings, llm
from core.analyze import utils, index
from core.analyze.analyzer import PythonAnalyzer, CppAnalyzer, CodeElementType
from core.analyze.index import FileDetails
from core.console import console
from core.db.milvus import MilvusManager
from core.embedding import EmbeddingModel
from core.llm import call_gemini_api
from core.log import logger
from core.thread import get_backend_thread_pool
from core.utils import strings
from core.utils.github import parse_repository_url

embedding_model = EmbeddingModel()
milvus_manager = MilvusManager(settings.get_milvus_uri(), "")


class CodeAnalyzer:
    def __init__(self, repo_fullname: str, milvus_uri: Optional[str] = None):
        if repo_fullname.startswith("http"):
            repo = parse_repository_url(repo_fullname)
            repo_fullname = repo.get_repo_fullname()
        self.repo_fullname = repo_fullname
        self.project_url = f"https://github.com/{repo_fullname}"
        self.base_data_path = os.path.join(settings.BASE_PATH, './data')
        self.project_source_path = os.path.join(self.base_data_path, '.source', repo_fullname)
        self.analyze_data_path = os.path.join(self.base_data_path, '.analyze', repo_fullname)
        self.index_manager = index.get_index_manager(repo_fullname, self.base_data_path, self.project_source_path)
        self.dependencies = {}
        self.exclude_path = []
        self.milvus_uri = milvus_uri
        self.code_elements_collection = f"v1_code_{self.repo_fullname.replace('/', '_').lower()}"
        self.code_elements_collection_loaded = False
        self.init_lock = asyncio.Lock()

        # Initialize language-specific analyzers
        self.analyzers = {
            'python': PythonAnalyzer(self.project_source_path),
            'cpp': CppAnalyzer(self.project_source_path),
            'c': CppAnalyzer(self.project_source_path)  # We can use the same analyzer for C and C++
        }
        self.update_exclude_path(None)

    @staticmethod
    def can_use(repo_fullname: str) -> bool:
        """
        如果索引目录存在，则可以使用
        :return:
        """
        index_path = index.get_index_path(repo_fullname, os.path.join(settings.BASE_PATH, './data'))
        return os.path.exists(index_path)

    async def check_elements_collection(self) -> bool:
        """
        获取或创建代码元素集合
        """
        if self.code_elements_collection_loaded:
            return True
        if await milvus_manager.has_collection(self.code_elements_collection):
            self.code_elements_collection_loaded = True
            return True

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=500),
            FieldSchema(name="language", dtype=DataType.VARCHAR, max_length=20),
            FieldSchema(name="element_type", dtype=DataType.VARCHAR, max_length=20),
            FieldSchema(name="element_name", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=768)
        ]
        schema = CollectionSchema(fields=fields, description="Code search collection")
        await milvus_manager.create_collection(
            dimension=768,
            metric_type="IP",
            collection_name=self.code_elements_collection,
            schema=schema,
            vector_field_name="embedding",
            description="Code search collection"
        )
        # 创建向量索引
        index_params = IndexParams()
        try:
            # 判断milvus使用的模式, 本地或者内存
            if self.milvus_uri == "sqlite://:memory:" or not self.milvus_uri or self.milvus_uri.startswith("/"):
                index_params.add_index("embedding", "FLAT", "embedding_index", metric_type="IP")
            else:
                index_params.add_index("embedding", "IVF_FLAT", "embedding_index", nlist=1024, metric_type="IP")
            await milvus_manager.create_index(
                collection_name=self.code_elements_collection,
                index_params=index_params
            )
        except Exception as e:
            logger.error(f"Failed to create index: {e}")
            # 删除集合
            await milvus_manager.drop_collection(self.code_elements_collection)
            raise e
        await milvus_manager.load_collection(self.code_elements_collection)
        self.code_elements_collection_loaded = True
        return True

    def get_code_files(self) -> List[str]:
        """
        获取所有支持的代码文件的路径
        """
        # files = []
        # for ext in utils.SUPPORTED_LANGUAGES_EXTENSIONS.keys():
        #     files.extend(glob.glob(f"{self.project_source_path}/**/*{ext}", recursive=True))
        # for exclude_dir in self.exclude_path:
        #     files = [f for f in files if exclude_dir not in f]
        # return files
        pattern = os.path.join(self.project_source_path, '**', '*.*')
        files = [
            f for f in glob.iglob(pattern, recursive=True)
            if os.path.splitext(f)[1] in utils.SUPPORTED_LANGUAGES_EXTENSIONS.keys()
               and not any(exclude in f for exclude in self.exclude_path)
        ]
        return files

    def has_source_code(self):
        """
        检查是否有源代码
        :return:
        """
        if os.path.exists(self.project_source_path) and os.path.isdir(self.project_source_path):
            return True
        return False

    def update_exclude_path(self, exclude_dirs: List[str] | None = None):
        exclude_path_file = os.path.join(self.analyze_data_path, "exclude_dirs.json")
        if not os.path.exists(os.path.dirname(exclude_path_file)):
            os.makedirs(os.path.dirname(exclude_path_file))
        if exclude_dirs:
            self.exclude_path = exclude_dirs
            # 写入到文件
            with open(exclude_path_file, "w") as f:
                json.dump(self.exclude_path, f)
        else:
            # 加载排除的目录
            if os.path.exists(exclude_path_file):
                with open(exclude_path_file, "r") as f:
                    self.exclude_path = json.load(f)
        print(f"Exclude path: {self.exclude_path}")

    def git_clone(self) -> bool:
        """
        克隆代码
        """

        def progress_callback(op_code, cur_count, max_count=None, message=''):
            if max_count:
                progress.update(main_task, completed=int(cur_count / max_count * 100))

        logger.info("checking out the code")
        if not self.has_source_code():
            try:
                logger.info(f"Cloning {self.project_url} to {self.project_source_path}")
                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=console
                )
                with progress:
                    main_task = progress.add_task("[green]Cloning main repository...", total=100)
                    repo = git.Repo.clone_from(self.project_url, self.project_source_path, progress=progress_callback)
                    progress.update(main_task, completed=100)
                    # 如果有子项目，也需要初始化
                    # 初始化和更新子模块
                    submodules = repo.submodules
                    submodule_task = progress.add_task("[cyan]Updating submodules...", total=len(submodules))
                    for submodule in submodules:
                        progress.console.print(f"Updating submodule: {submodule.name}")
                        submodule.update(init=True, recursive=True)
                        progress.advance(submodule_task)
            except Exception as e:
                logger.error(f"Failed to clone the repository: {e}")
                # 删除目录以及目录下的文件
                if os.path.exists(self.project_source_path):
                    shutil.rmtree(self.project_source_path)
                return False
            return True
        else:
            logger.info(f"Pulling latest changes for {self.project_url}")
            repo = git.Repo(self.project_source_path)
            repo.remotes.origin.pull()

    async def make_full_index(self, exclude_dirs: List[str] = None):
        """
        创建完整的代码索引
        :return:
        """
        self.update_exclude_path(exclude_dirs)
        self.git_clone()
        logger.info("Cleaning up the index")
        self.index_manager.clean_index()
        summary = {
            "total_files": 0,
            "languages": {},
            "total_code_elements": 0,
            "element_types": {},
            "dependencies": self.dependencies
        }
        embedding_model.get_model()
        logger.info("Analyzing code files")
        with Progress(transient=True) as progress:
            files = self.get_code_files()
            task = progress.add_task("[cyan]Analyzing...", total=len(files), start=True)
            for file_path in files:
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
                    file_index_name = os.path.relpath(file_path, self.project_source_path)
                    progress.update(task, advance=0, description=f"Analyzing {file_index_name}...")
                    file_detail = self.get_file_detail(file_path, content)
                    if file_detail:
                        progress.update(task, advance=0, description=f"Make index[{len(file_detail.code_elements)}] for"
                                                                     f" {file_index_name}...")
                        self.index_manager.insert_or_update(file_detail)
                        await self.save_to_db(file_detail)
                    summary["total_files"] += 1
                    summary["languages"][file_detail.language] = summary["languages"].get(file_detail.language, 0) + 1
                    summary["total_code_elements"] += len(file_detail.code_elements)
                    self.dependencies[file_detail.file_name] = file_detail.dependencies
                    for element in file_detail.code_elements:
                        element_type = element['type']
                        summary["element_types"][element_type] = summary["element_types"].get(element_type, 0) + 1
                progress.update(task, advance=1, description=f"Analyzed {file_index_name}")
        if self.index_manager.make_structure(self.get_code_files()):
            self.index_manager.save_structure_to_json()

        # 生成项目摘要
        summary = await self.generate_project_summary(summary)
        if summary:
            summary_path = os.path.join(self.analyze_data_path, "project_summary.json")
            if not os.path.exists(os.path.dirname(summary_path)):
                os.makedirs(os.path.dirname(summary_path))
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            overview = summary.get("project_overview", "")
            if overview:
                with open(os.path.join(self.analyze_data_path, "project_overview.md"), "w") as f:
                    f.write(overview)

    def get_file_detail(self, file_path: str, content: str) -> FileDetails | None:
        file_name_for_index = os.path.relpath(file_path, self.project_source_path)
        code_hash = strings.get_content_hash(content)
        language = utils.get_support_file_language(file_path)
        analyzer = self.analyzers.get(language)
        if not analyzer:
            return None
        code_elements = analyzer.extract_code_elements(file_path, content)
        dependencies = analyzer.analyze_dependencies(file_path, content)
        file_detail = index.FileDetails(
            file_name=file_name_for_index,
            code_hash=code_hash,
            language=language,
            file_path=file_path,
            dependencies=dependencies,
            code_elements=code_elements
        )
        return file_detail

    async def save_to_db(self, file_detail: FileDetails):
        """
        保存文件详情到数据库
        :param file_detail:
        :return:
        """
        added_set = set()
        try:
            # 删除此文件的旧向量
            await self.check_elements_collection()
            await milvus_manager.delete(collection_name=self.code_elements_collection,
                                        filter=f"file_path == '{file_detail.file_name}'"
                                        )
            # 插入新向量
            data = []
            # if len(file_detail.code_elements) > 60:
            #     logger.info(f"Too many code elements in {file_detail.file_name}, only saving the first 60.")
            exclude_types_list = [CodeElementType.CONSTANT, CodeElementType.VARIABLE]
            for element in file_detail.code_elements:
                if not element['name'] or len(element['name']) == 0:
                    continue
                if element['type'] in exclude_types_list:
                    continue
                if f'{element["type"]}_{element["name"]}' in added_set:
                    continue
                embedding = await embedding_model.async_encode_text(element['name'])
                data.append({
                    "file_path": file_detail.file_name,
                    "language": file_detail.language,
                    "element_type": element['type'],
                    "element_name": element['name'],
                    "content": element['content'][:18000],
                    "embedding": embedding.tolist()
                })

            await milvus_manager.insert(collection_name=self.code_elements_collection, data=data)
        except MilvusException as e:
            logger.error(f"Failed to save file details to the database: {e}")
            await milvus_manager.release_client()
        except Exception as e:
            logger.error(f"Failed to save file details to the database: {e}", exc_info=True, stack_info=True)
            await milvus_manager.release_client()

    async def analyze_code(self, file_path: str, file_content: str, is_delete: bool):
        """
        分析单个文件
        """
        file_name_for_index = os.path.relpath(file_path, self.project_source_path)
        if is_delete:
            # 删除文件
            logger.info(f"Deleting file {file_name_for_index}")
            self.index_manager.delete(file_name_for_index)
            await self.check_elements_collection()
            await milvus_manager.delete(collection_name=self.code_elements_collection,
                                        filter=f"file_path == '{file_name_for_index}'"
                                        )
        code_hash = strings.get_content_hash(file_content)
        index_detail = self.index_manager.get_index(file_name_for_index)
        # 检查文件是否有变化
        if not index_detail or index_detail.code_hash != code_hash:
            logger.info(f"Analyzing file {file_name_for_index}")
            file_detail = self.get_file_detail(file_path, file_content)
            if file_detail:
                self.index_manager.insert_or_update(file_detail)
                await self.save_to_db(file_detail)
                logger.info(f"Analyzed file {file_name_for_index}")
            else:
                logger.error(f"Failed to analyze file {file_name_for_index}")
        else:
            logger.info(f"File {file_name_for_index} has not changed.")

    async def check_git_changes(self):
        """
        检查git的变化
        """
        if not os.path.exists(self.project_source_path):
            logger.error(f"Project source path {self.project_source_path} does not exist.")
            return
        repo = git.Repo(self.project_source_path)
        # Store the current commit hash
        old_commit = repo.head.commit
        # Pull the latest changes
        origin = repo.remotes.origin
        await get_backend_thread_pool().run_in_thread(origin.pull)
        # Get the new commit hash
        new_commit = repo.head.commit
        # Get the list of changed files
        changed_files = old_commit.diff(new_commit)
        for file in changed_files:
            # 判断文件是添加/修改还是删除
            if file.change_type in ['A', 'M']:
                is_delete = False
            elif file.change_type == 'D':
                is_delete = True
            else:
                continue
            file_path = os.path.abspath(os.path.join(self.project_source_path, file.a_path))
            if file.a_path.endswith(tuple(utils.SUPPORTED_LANGUAGES_EXTENSIONS.keys())):
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    await self.analyze_code(file_path, content, is_delete)

    async def get_db_count(self):
        """
        获取数据库中的元素数量
        """
        client = await milvus_manager.get_client(self.code_elements_collection)
        return client.get_collection_stats(self.code_elements_collection)["row_count"]

    async def generate_project_summary(self, summary: Dict) -> Dict[str, Any]:
        """
        生成项目摘要
        """
        # 添加一些统计信息
        summary["average_elements_per_file"] = summary["total_code_elements"] / summary["total_files"] if summary[
                                                                                                              "total_files"] > 0 else 0
        summary["language_distribution"] = {lang: count / summary["total_files"] * 100 for lang, count in
                                            summary["languages"].items()}
        summary["element_type_distribution"] = {elem_type: count / summary["total_code_elements"] * 100 for
                                                elem_type, count in summary["element_types"].items()}
        # 尝试读取项目根目录下的README文件
        readme_path = os.path.join(self.project_source_path, "README.md")
        if os.path.exists(readme_path):
            with open(readme_path, 'r', encoding='utf-8') as f:
                summary["readme"] = f.read()
        else:
            return {}
        # 使用AI生成项目概览
        overview = await self.generate_project_overview(summary)
        summary["project_overview"] = overview
        return summary

    async def generate_project_overview(self, summary: Dict[str, Any]) -> str:
        """
        使用Gemini生成项目概览
        """
        prompt = f"""
        Based on the following project summary, please provide a concise overview of the project:

        {json.dumps(summary, indent=2)}
        
        ---
        
        1. You need to answer in English.
        2. The overview you provide is not for humans, but for other AIs to easily understand the project.
        3. The overview you provide cannot exceed 250 characters. If it exceeds, please reflect and rewrite it.
        """
        messages = [{"role": "user", "content": prompt}]

        overview = await llm.call_ai_api(
            "As an AI assistant, generate a project overview based on the provided summary. ",
            messages, settings.REVIEW_MODEL, 0.3, 50, 0.9)
        return overview

    def clean_patch(self, patch_content: str) -> str:
        """
        清理补丁内容，删除两个@@之间的字符, 忽略删除的行
        """
        cleaned_patch = []
        for line in patch_content.split('\n'):
            if line.startswith('@@'):
                cleaned_patch.append(line.rsplit('@@', 1)[1])
            elif line.startswith('-'):
                continue
            elif line.startswith('+'):
                cleaned_patch.append(line[1:])
            else:
                cleaned_patch.append(line)
        return '\n'.join(cleaned_patch)

    async def get_review_context(self, filename: str, patch_content: str) -> Dict[str, Any]:
        """
        审查所需要的上下文信息
        """
        patch_embedding = []
        patch_content = self.clean_patch(patch_content)
        language = utils.get_support_file_language(filename)
        analyzer = self.analyzers.get(language)
        code_elements = list(analyzer.extract_functions_from_patch(patch_content))
        if not code_elements or len(code_elements) == 0:
            code_elements = patch_content.split("\n")
        code_elements_count = len(code_elements)
        limit = 20 // code_elements_count
        if limit < 1:
            limit = 1
            code_elements = code_elements[:20]
        for element in code_elements:
            element_v = await embedding_model.async_encode_text(element)
            patch_embedding.append(element_v.tolist())
        search_params = {"metric_type": "IP", "params": {"nprobe": 10}}
        await self.check_elements_collection()
        results = await milvus_manager.search(
            collection_name=self.code_elements_collection,
            # filter="element_name in ['" + "','".join(code_elements) + "']",
            # filter="element_name != ''",
            data=patch_embedding,
            anns_field="embedding",
            search_params=search_params,
            limit=limit,
            output_fields=["file_path", "language", "element_type", "element_name", "content"]
        )
        related_elements = []
        for result in results:
            if isinstance(result, dict):
                continue
            for code_element in result:
                related_elements.append(code_element)

        # 获取相关元素的上下文信息
        context_info = self.get_context_info(related_elements)
        # 分析补丁中的依赖关系
        patch_dependencies = self.get_dependencies(filename)
        logger.info("Dependencies: %s", patch_dependencies)
        # 项目的概述 "project_overview.md"
        project_overview = ""
        overview_path = os.path.join(self.analyze_data_path, "project_overview.md")
        if os.path.exists(overview_path):
            with open(overview_path, 'r') as f:
                project_overview = f.read()
        return {
            "context_info": context_info,
            "dependencies": patch_dependencies,
            "overview": project_overview
        }

    def get_context_info(self, related_elements: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        获取补丁相关的上下文信息
        """
        context = {}

        for entity in related_elements:
            element = entity['entity']
            file_path = element['file_path']
            if file_path not in context:
                context[file_path] = {
                    "language": element['language'],
                }
                for elem_type in CodeElementType:
                    context[file_path][elem_type.value] = {}
            context[file_path][element['element_type']][element['element_name']] = element['content']
        return context

    def get_dependencies(self, filename: str) -> Dict[str, str]:
        """
        分析补丁中的依赖关系
        """
        result = {}
        index_detail = self.index_manager.get_index(filename)
        if not index_detail:
            return result
        for file_name in index_detail.dependencies:
            if len(result) > 6:
                return result
            # 读取依赖文件的内容
            file_path = os.path.join(self.project_source_path, file_name)
            if not os.path.exists(file_path):
                continue
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                result[file_name] = content
        return result

    def generate_gemini_review(self, patch_content: str,
                               context_info: Dict[str, Any], patch_dependencies: List[str]) -> Dict[str, Any]:
        """使用Gemini-1.5生成代码审查"""
        prompt = f"""
            作为一个经验丰富的软件工程师，请审查以下代码补丁：

            {patch_content}

            相关的上下文信息：
            {json.dumps(context_info, indent=2, ensure_ascii=False)}

            补丁的依赖关系：
            {json.dumps(patch_dependencies, indent=2, ensure_ascii=False)}

            请提供以下方面的审查：
            1. 代码质量：评估代码的可读性、简洁性和效率。
            2. 潜在问题：指出任何可能的bug、安全隐患或性能问题。
            3. 最佳实践：建议如何改进代码以符合行业最佳实践。
            4. 与相关文件的一致性：评估这个补丁是否与相关文件的风格和结构一致。
            5. 上下文分析：分析补丁中使用的函数和变量是否与已有代码保持一致，是否有任何潜在的冲突或改进空间。
            6. 总体评价：给出对这个补丁的总体评价，包括是否建议合并。

            请以JSON格式提供您的审查结果。
            """
        message = {"content": prompt}
        review_text = asyncio.run(call_gemini_api("As an expert code reviewer, analyze the provided code and offer "
                                                  "constructive feedback. ", message, settings.REVIEW_MODEL, 0.2, 40,
                                                  0.8))
        review_json = json.loads(review_text)

        return review_json

    def suggest_code_improvements(self, patch_content: str, review: Dict[str, Any]) -> Dict[str, Any]:
        """根据代码审查结果提出改进建议"""
        prompt = f"""
        根据以下代码审查结果，请提供具体的改进建议：

        {json.dumps(review, indent=2, ensure_ascii=False)}

        代码补丁：
        {patch_content}

        请提供以下方面的具体改进建议：
        1. 代码重构：如何重构代码以提高可读性和可维护性。
        2. 性能优化：如何优化代码以提高性能。
        3. 安全性增强：如何增强代码的安全性。
        4. 错误处理：如何改进错误处理和异常管理。
        5. 文档和注释：如何改进代码文档和注释。

        请以JSON格式提供您的改进建议。
        """

        message = {"content": prompt}
        suggestions_text = asyncio.run(
            call_gemini_api("As an expert code reviewer, analyze the provided code and offer "
                            "constructive feedback. ", message, settings.REVIEW_MODEL, 0.2, 40,
                            0.8))
        suggestions_json = json.loads(suggestions_text)

        return suggestions_json
