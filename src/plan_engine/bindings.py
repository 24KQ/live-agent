"""Phase 12A 节点输入绑定的受控物化边界。

候选 DAG 只声明从哪里读取参数，本模块在真正派发前把声明解析成普通 JSON。
它不读取 Store、Worker 或外部服务，因此不会绕过版本、依赖与审计边界。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Any

from src.plan_engine.models import (
    CardBatchPlanningInput,
    FrozenDict,
    FrozenList,
    InputBinding,
    InputBindingKind,
)


class PlanValidationError(ValueError):
    """表示候选绑定或其运行时输入违反 PlanEngine 的受控读取规则。

    该异常刻意不包含完整业务快照，避免调用方在错误处理、日志或 API 响应中
    泄漏商品参数；消息只说明哪一种计划约束没有被满足。
    """


@dataclass(frozen=True)
class VersionedNodeOutput:
    """带 PlanVersion 归属的上游输出，用于阻止版本间的隐式数据复用。

    每个已完成节点的运行时输出都必须封装为此值：``plan_version`` 记录输出所属的
    计划版本，``output`` 保存可沿静态路径读取的 JSON 数据。解析器据此实施强制的
    同版本读取校验，不接受任何缺少版本归属的裸值。
    """

    plan_version: int
    output: Any


@dataclass(frozen=True)
class MaterializedNodeInput:
    """派发前冻结的普通 JSON 参数与其规范 SHA-256 输入指纹。

    指纹基于所有参数的规范 JSON 而非 Python 对象身份，因而可被 NodeRun 持久化、
    跨进程比对并用于审计重放。这个值对象不表达任何 Worker 调度行为。
    """

    parameters: dict[str, Any]
    input_fingerprint: str

    def __post_init__(self) -> None:
        """在值对象自身的构造边界封闭严格 JSON、不可变性与指纹一致性。

        调用方可能绕过 ``InputBindingResolver.materialize`` 直接实例化本对象，因此
        不能依赖 resolver 的正常物化路径预先建立不变量。这里直接复用 resolver 的
        同一组规范化原语，避免严格 JSON 规则、冻结策略或哈希编码产生两套实现。
        """
        canonical_parameters = InputBindingResolver._copy_json(
            self.parameters,
            "物化参数不是普通 JSON",
        )
        immutable_parameters = InputBindingResolver._freeze_json_snapshot(
            canonical_parameters
        )
        expected_fingerprint = InputBindingResolver._canonical_sha256(
            immutable_parameters
        )
        if self.input_fingerprint != expected_fingerprint:
            raise PlanValidationError("输入指纹与物化参数的规范 JSON 不一致")

        # frozen dataclass 禁止普通字段赋值；校验完成后仅在构造期写入与外部引用
        # 隔离的深度冻结快照，使任何成功实例都始终满足参数与指纹一一对应。
        object.__setattr__(self, "parameters", immutable_parameters)


class InputBindingResolver:
    """仅按静态 tuple 路径解析 PLAN_INPUT、NODE_OUTPUT 和 LITERAL。

    解析器不能执行表达式、属性访问或环境变量展开。所有路径段都以 JSON object key
    或 JSON array index 的方式解释，从而让候选 Provider 无法借由参数绑定扩大读取面。
    对 NODE_OUTPUT 的读取还必须同时满足显式依赖、输出存在和 PlanVersion 精确一致，
    任一版本事实缺失时均采用 fail-closed，避免旧 DAG 输出被新计划隐式复用。
    """

    def resolve(
        self,
        binding: InputBinding,
        planning_input: CardBatchPlanningInput,
        dependency_outputs: Mapping[str, Any],
        declared_dependencies: frozenset[str],
        *,
        current_plan_version: int | None = None,
    ) -> Any:
        """解析一个绑定，返回与输入源断开引用关系的普通 JSON 值。

        解析 NODE_OUTPUT 时，``dependency_outputs`` 中的值必须使用
        ``VersionedNodeOutput`` 携带归属版本，并且调用方必须提供
        ``current_plan_version``。版本事实缺失或不匹配均 fail-closed。
        """
        self._validate_runtime_path(binding.path)
        kind = self._binding_kind(binding)
        if kind is InputBindingKind.LITERAL:
            if binding.path or binding.upstream_logical_key is not None:
                raise PlanValidationError("LITERAL 绑定不能携带路径或上游依赖")
            return self._copy_json(binding.literal_value, "LITERAL 值不是普通 JSON")

        if not binding.path:
            raise PlanValidationError("绑定路径不能为空")

        if kind is InputBindingKind.PLAN_INPUT:
            if binding.upstream_logical_key is not None:
                raise PlanValidationError("PLAN_INPUT 不能引用上游节点")
            # model_dump(mode="json") 将 Decimal 等领域值投影为 JSON，保证解析器绝不
            # 把 Pydantic 模型、冻结容器或领域对象直接交给后续执行层。
            source = planning_input.model_dump(mode="json")
        elif kind is InputBindingKind.NODE_OUTPUT:
            source = self._node_output_source(
                binding=binding,
                dependency_outputs=dependency_outputs,
                declared_dependencies=declared_dependencies,
                current_plan_version=current_plan_version,
            )
        else:  # pragma: no cover - _binding_kind 已封闭枚举，保留防御性分支。
            raise PlanValidationError("未知输入绑定来源")

        return self._copy_json(
            self._traverse_path(source, binding.path),
            "绑定解析结果不是普通 JSON",
        )

    def materialize(
        self,
        input_bindings: Mapping[str, InputBinding],
        planning_input: CardBatchPlanningInput,
        dependency_outputs: Mapping[str, Any],
        declared_dependencies: frozenset[str],
        *,
        current_plan_version: int | None = None,
    ) -> MaterializedNodeInput:
        """物化一个节点的全部普通 JSON 参数并生成稳定输入指纹。

        参数名也在此边界验证，避免非字符串 key 被 JSON 编码悄悄转换。按原映射顺序
        解析不影响最终指纹，因为规范编码固定使用 ``sort_keys=True``。
        """
        parameters: dict[str, Any] = {}
        for parameter_name, binding in input_bindings.items():
            if not isinstance(parameter_name, str) or not parameter_name:
                raise PlanValidationError("输入参数名必须是非空字符串")
            if not isinstance(binding, InputBinding):
                raise PlanValidationError("输入参数必须使用 InputBinding 声明")
            parameters[parameter_name] = self.resolve(
                binding=binding,
                planning_input=planning_input,
                dependency_outputs=dependency_outputs,
                declared_dependencies=declared_dependencies,
                current_plan_version=current_plan_version,
            )

        canonical_parameters = self._copy_json(parameters, "物化参数不是普通 JSON")
        immutable_parameters = self._freeze_json_snapshot(canonical_parameters)
        return MaterializedNodeInput(
            parameters=immutable_parameters,
            input_fingerprint=self._canonical_sha256(immutable_parameters),
        )

    @staticmethod
    def _binding_kind(binding: InputBinding) -> InputBindingKind:
        """把可能由 ``model_construct`` 绕过的枚举值重新收敛到受控来源集合。"""
        try:
            return InputBindingKind(binding.kind)
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("未知输入绑定来源") from exc

    @staticmethod
    def _node_output_source(
        binding: InputBinding,
        dependency_outputs: Mapping[str, Any],
        declared_dependencies: frozenset[str],
        current_plan_version: int | None,
    ) -> Any:
        """按固定顺序校验显式依赖、输出存在性和强制的 PlanVersion 归属。

        先验证 DAG 声明与节点完成事实，可以为调用方保留准确的依赖错误；只有输出
        确实存在后才检查其版本包装、当前版本参数和版本一致性。任何裸 JSON 值都不
        具备可审计的版本归属，因此不能进入后续路径遍历。
        """
        upstream_key = binding.upstream_logical_key
        if not upstream_key:
            raise PlanValidationError("NODE_OUTPUT 必须提供上游节点")
        if upstream_key not in declared_dependencies:
            raise PlanValidationError(f"NODE_OUTPUT 引用未声明依赖: {upstream_key}")
        if upstream_key not in dependency_outputs:
            raise PlanValidationError(f"上游节点尚无输出: {upstream_key}")

        supplied_output = dependency_outputs[upstream_key]
        if not isinstance(supplied_output, VersionedNodeOutput):
            raise PlanValidationError("NODE_OUTPUT 上游输出缺少版本事实")
        if current_plan_version is None:
            raise PlanValidationError("读取带版本上游输出时必须提供当前计划版本")
        output_plan_version = InputBindingResolver._validate_plan_version(
            supplied_output.plan_version,
            "上游输出计划版本",
        )
        validated_current_plan_version = InputBindingResolver._validate_plan_version(
            current_plan_version,
            "当前计划版本",
        )
        if output_plan_version != validated_current_plan_version:
            raise PlanValidationError("禁止跨版本读取上游节点输出")
        return supplied_output.output

    @staticmethod
    def _validate_plan_version(value: Any, field_name: str) -> int:
        """在版本比较前收紧来自运行时边界的正整数事实。

        Python 中 ``bool`` 是 ``int`` 的子类，``True == 1``，整数值浮点数也会与
        int 相等；若直接比较会让错误类型穿过 PlanVersion 信任边界。因此必须使用
        精确类型判断，并在一致性比较前拒绝零和负数。
        """
        if type(value) is not int or value < 1:
            raise PlanValidationError(f"{field_name}必须是大于等于 1 的精确 int")
        return value

    @staticmethod
    def _validate_runtime_path(path: Any) -> None:
        """重验路径形状，封住 ``model_construct`` 绕过 Pydantic 字段校验的入口。"""
        if not isinstance(path, tuple):
            raise PlanValidationError("绑定路径必须是 tuple")
        if any(isinstance(part, bool) or not isinstance(part, (str, int)) for part in path):
            raise PlanValidationError("绑定路径只能包含字符串键或整数下标")
        if any(isinstance(part, str) and not part for part in path):
            raise PlanValidationError("绑定路径不能包含空字符串")
        if any(isinstance(part, int) and part < 0 for part in path):
            raise PlanValidationError("绑定路径下标不能为负数")

    @staticmethod
    def _traverse_path(source: Any, path: tuple[str | int, ...]) -> Any:
        """只使用 dict/list 的有限路径遍历，拒绝索引越界和非 JSON 容器。"""
        current = source
        for path_part in path:
            if isinstance(current, dict):
                if not isinstance(path_part, str):
                    raise PlanValidationError("JSON 对象路径必须使用字符串键")
                if path_part not in current:
                    raise PlanValidationError(f"绑定路径不存在: {path_part}")
                current = current[path_part]
                continue
            if isinstance(current, list):
                if isinstance(path_part, bool) or not isinstance(path_part, int):
                    raise PlanValidationError("JSON 数组路径必须使用非负整数下标")
                if path_part < 0 or path_part >= len(current):
                    raise PlanValidationError(f"绑定数组下标越界: {path_part}")
                current = current[path_part]
                continue
            raise PlanValidationError("绑定路径不能穿过非 JSON 容器")
        return current

    @staticmethod
    def _freeze_json_snapshot(value: Any) -> Any:
        """递归冻结已验证的 JSON 树，同时保留 ``dict``/``list`` 与序列化语义。

        ``MaterializedNodeInput`` 的 frozen dataclass 只能禁止字段整体重新赋值，无法
        阻止普通容器的原地写入。这里在指纹计算前关闭每一层 object/array 的变异
        入口，使返回给 Worker 的参数与指纹始终描述同一个不可变审计快照。
        """
        if isinstance(value, dict):
            return FrozenDict(
                {
                    key: InputBindingResolver._freeze_json_snapshot(item)
                    for key, item in value.items()
                }
            )
        if isinstance(value, list):
            return FrozenList(
                InputBindingResolver._freeze_json_snapshot(item) for item in value
            )
        return value

    @staticmethod
    def _canonical_sha256(value: Any) -> str:
        """按物化输入既有的规范 JSON 编码生成稳定 SHA-256 指纹。

        调用方必须先通过严格 JSON 复制；此方法只集中键排序、紧凑分隔符和 NaN
        拒绝选项，让 resolver 正常物化与值对象直接构造使用完全相同的摘要规则。
        """
        serialized = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _copy_strict_json(value: Any, active_container_ids: set[int]) -> Any:
        """在调用 JSON 编码器前递归验证值域，并构造无共享容器的普通副本。

        ``json.dumps`` 会把 tuple 当作 array，并把 int/bool/None object key 转成
        字符串；这些便利转换在输入信任边界会改变业务事实，甚至把 ``1`` 与 ``"1"``
        合并为同一键。因此这里只接受 JSON 原生标量以及 dict/list 容器，并显式检查
        非有限浮点和循环引用，不把规范化决策委托给编码器。
        """
        if value is None or type(value) in {str, bool, int}:
            return value
        if type(value) is float:
            if not isfinite(value):
                raise ValueError("JSON 浮点数必须是有限值")
            return value
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TypeError("JSON object key 必须是字符串")
            container_id = id(value)
            if container_id in active_container_ids:
                raise ValueError("JSON 容器不能包含循环引用")
            active_container_ids.add(container_id)
            try:
                return {
                    key: InputBindingResolver._copy_strict_json(
                        item,
                        active_container_ids,
                    )
                    for key, item in value.items()
                }
            finally:
                active_container_ids.remove(container_id)
        if isinstance(value, list):
            container_id = id(value)
            if container_id in active_container_ids:
                raise ValueError("JSON 容器不能包含循环引用")
            active_container_ids.add(container_id)
            try:
                return [
                    InputBindingResolver._copy_strict_json(
                        item,
                        active_container_ids,
                    )
                    for item in value
                ]
            finally:
                active_container_ids.remove(container_id)
        raise TypeError(f"值不是严格 JSON 类型: {type(value).__name__}")

    @staticmethod
    def _copy_json(value: Any, error_message: str) -> Any:
        """先执行严格 JSON 递归校验，再往返复制以隔离所有外部对象引用。"""
        try:
            validated_copy = InputBindingResolver._copy_strict_json(value, set())
            serialized = json.dumps(validated_copy, allow_nan=False)
            return json.loads(serialized)
        except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
            raise PlanValidationError(error_message) from exc
