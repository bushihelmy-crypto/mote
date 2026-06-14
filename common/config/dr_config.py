from metagpt.common.utils.yaml_model import YamlModel


class DrLeaderConfig(YamlModel):
    """Config for DrLeader"""

    do_intent_check: bool = False
    do_insert_image: bool = False
    do_insert_chart: bool = True
    do_search_plan_revise: bool = True
    do_init_search: bool = False
    max_ds_depth: int = 1
    max_dr_react_loop: int = 10
    max_ic_react_loop: int = 6


class DrSearchConfig(YamlModel):
    """Config for DrSearch"""

    compact_method: str = None
    search_type: str = "tavily_only"
    api_config: dict = {}
    max_chars_per_page: int = 40000


class DrWriterConfig(YamlModel):
    """Config for DrWriter"""

    image_gen_base_url: str = ""
    image_gen_api_key: str = ""
    chart_size: str = "1024x1024"
    image_size: str = "1024x1024"
