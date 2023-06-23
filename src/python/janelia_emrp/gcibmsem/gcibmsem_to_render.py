import argparse
import csv
import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Any, Optional

import renderapi
from PIL import Image
from renderapi import Render
from renderapi.errors import RenderError

from janelia_emrp.fibsem.render_api import RenderApi
from janelia_emrp.fibsem.volume_transfer_info import params_to_render_connect
from janelia_emrp.gcibmsem.field_of_view_layout \
    import NINETY_ONE_SFOV_NAME_TO_ROW_COL, FieldOfViewLayout, NINETEEN_MFOV_COLUMN_GROUPS
from janelia_emrp.gcibmsem.scan_fit_parameters import load_scan_fit_parameters, ScanFitParameters
from janelia_emrp.gcibmsem.slab_info import SlabInfo
from janelia_emrp.gcibmsem.wafer_info import load_wafer_info, WaferInfo, build_wafer_info_parent_parser

program_name = "gcibmsem_to_render.py"

# set up logging
logger = logging.getLogger(program_name)
c_handler = logging.StreamHandler(sys.stdout)
c_formatter = logging.Formatter("%(asctime)s [%(threadName)s] [%(name)s] [%(levelname)s] %(message)s")
c_handler.setFormatter(c_formatter)
logger.addHandler(c_handler)
logger.setLevel(logging.INFO)

render_api_logger = logging.getLogger("renderapi")
render_api_logger.setLevel(logging.DEBUG)

WAFER_53_LAYOUT = FieldOfViewLayout(NINETEEN_MFOV_COLUMN_GROUPS, NINETY_ONE_SFOV_NAME_TO_ROW_COL)


def build_tile_spec(image_path: Path,
                    stage_x: int,
                    stage_y: int,
                    stage_z: int,
                    tile_id: str,
                    tile_width: int,
                    tile_height: int,
                    mfov_name: str,
                    sfov_index_name: str,
                    min_x: int,
                    min_y: int,
                    scan_fit_parameters: ScanFitParameters,
                    margin: int) -> dict[str, Any]:

    # TODO: need to get and save working distance

    section_id = f'{stage_z}.0'
    image_row, image_col = WAFER_53_LAYOUT.row_and_col(mfov_name, sfov_index_name)

    mipmap_level_zero = {"imageUrl": f'file:{image_path}'}

    transform_data_string = f'1 0 0 1 {stage_x - min_x + margin} {stage_y - min_y + margin}'

    tile_spec = {
        "tileId": tile_id, "z": stage_z,
        "layout": {
            "sectionId": section_id,
            "imageRow": image_row, "imageCol": image_col,
            "stageX": stage_x, "stageY": stage_y
        },
        "width": tile_width, "height": tile_height, "minIntensity": 0, "maxIntensity": 255,
        "mipmapLevels": {
            "0": mipmap_level_zero
        },
        "transforms": {
            "type": "list",
            "specList": [
                scan_fit_parameters.to_transform_spec(),
                {"className": "mpicbg.trakem2.transform.AffineModel2D", "dataString": transform_data_string}
            ]
        }
    }

    return tile_spec


# unix_relative_image_path: 000003/002_000003_001_2022-04-01T1723012239596.png
unix_relative_image_path_pattern = re.compile(r"(^\d+)/(\d{3}_\d{6}_(\d{3})_\d{4}-\d{2}-\d{2}T\d{13}).png$")


def build_tile_specs_for_slab_scan(slab_scan_path: Path,
                                   slab_info: SlabInfo) -> list[dict[str, Any]]:

    scan_fit_parameters = load_scan_fit_parameters(slab_scan_path)
    stage_z = slab_info.first_scan_z + scan_fit_parameters.scan_index

    tile_data = []
    tile_width = None
    tile_height = None
    min_x = None
    min_y = None
    full_image_coordinates_path = Path(slab_scan_path, "full_image_coordinates.txt")

    if full_image_coordinates_path.exists():
        with open(full_image_coordinates_path, 'r') as data_file:
            # 000007\020_000007_082_2022-04-03T0154134018404.png	2014641.659	915550.903	0
            for row in csv.reader(data_file, delimiter="\t"):
                unix_relative_image_path = row[0].replace('\\', '/')
                stage_x = int(float(row[1]))
                stage_y = int(float(row[2]))
                image_path = Path(slab_scan_path, unix_relative_image_path)

                unix_relative_image_path_match = unix_relative_image_path_pattern.match(unix_relative_image_path)
                if not unix_relative_image_path_match:
                    raise RuntimeError(f"failed to parse unix_relative_image_path {unix_relative_image_path} "
                                       f"in {full_image_coordinates_path}")

                mfov_name = unix_relative_image_path_match.group(1)

                # Slightly shorten/simplify tile id so that it works better with web UIs.
                # Technically, scan timestamp could be completely removed because stage_z gets appended to tile_id.
                # Decided to keep scan time with truncated microseconds in the id because it is nice context to have.
                # Example shortening: 020_000007_082_2022-04-03T0154134018404 => 020_000007_082_20220403_015413
                short_sfov_name = unix_relative_image_path_match.group(2).replace("-", "").replace("T", "_")[:-7]
                tile_id = f"{short_sfov_name}.{stage_z}.0"

                sfov_index_name = unix_relative_image_path_match.group(3)

                if not tile_width:
                    image = Image.open(image_path)
                    tile_width = image.width
                    tile_height = image.height
                    min_x = stage_x
                    min_y = stage_y
                else:
                    min_x = min(min_x, stage_x)
                    min_y = min(min_y, stage_y)

                tile_data.append(
                    (tile_id, mfov_name, sfov_index_name, image_path, stage_x, stage_y))
    else:
        logger.warning(f'{full_image_coordinates_path} not found')

    tile_specs = [
        build_tile_spec(image_path=image_path,
                        stage_x=stage_x,
                        stage_y=stage_y,
                        stage_z=stage_z,
                        tile_id=tile_id,
                        tile_width=tile_width,
                        tile_height=tile_height,
                        mfov_name=mfov_name,
                        sfov_index_name=sfov_index_name,
                        min_x=min_x,
                        min_y=min_y,
                        scan_fit_parameters=scan_fit_parameters,
                        margin=400)
        for (tile_id, mfov_name, sfov_index_name, image_path, stage_x, stage_y) in tile_data
    ]

    logger.info(f'loaded {len(tile_specs)} tile specs from {slab_scan_path}')

    return tile_specs


def get_stack_metadata_or_none(render: Render,
                               stack_name: str) -> Optional[dict[str, Any]]:
    stack_metadata = None
    try:
        stack_metadata = renderapi.stack.get_stack_metadata(render=render, stack=stack_name)
    except RenderError:
        print(f"failed to retrieve metadata for stack {stack_name}")
    return stack_metadata


def import_slab_stacks_for_wafer(render_ws_host: str,
                                 render_owner: str,
                                 wafer_info: WaferInfo,
                                 import_scan_name_list: list[str]):

    for slab_group in wafer_info.slab_group_list:

        render_connect_params = {
            "host": render_ws_host,
            "port": 8080,
            "owner": render_owner,
            "project": slab_group.to_render_project_name(),
            "web_only": True,
            "validate_client": False,
            "client_scripts": "/groups/flyTEM/flyTEM/render/bin",
            "memGB": "1G"
        }

        render = renderapi.connect(**render_connect_params)

        render_api = RenderApi(render_owner=render_connect_params["owner"],
                               render_project=render_connect_params["project"],
                               render_connect=params_to_render_connect(render_connect_params))

        for slab_info in slab_group.ordered_slabs:
            stack = slab_info.stack_name()

            stack_metadata = get_stack_metadata_or_none(render=render, stack_name=stack)

            if stack_metadata is None:
                # explicitly set createTimestamp until render-python bug is fixed
                # see https://github.com/AllenInstitute/render-python/pull/158
                create_timestamp = time.strftime('%Y-%m-%dT%H:%M:%S.00Z')
                renderapi.stack.create_stack(stack,
                                             render=render,
                                             createTimestamp=create_timestamp,
                                             stackResolutionX=wafer_info.resolution[0],
                                             stackResolutionY=wafer_info.resolution[1],
                                             stackResolutionZ=wafer_info.resolution[2])
            else:
                renderapi.stack.set_stack_state(stack, 'LOADING', render=render)

            for scan_path in wafer_info.scan_paths:
                if len(import_scan_name_list) == 0 or scan_path.name in import_scan_name_list:
                    slab_scan_path = Path(scan_path, slab_info.dir_name())
                    tile_specs = build_tile_specs_for_slab_scan(slab_scan_path, slab_info)
                    if len(tile_specs) > 0:
                        tile_id_range = f'{tile_specs[0]["tileId"]} to {tile_specs[-1]["tileId"]}'
                        logger.info(f"import_slab_stacks_for_wafer: saving tiles {tile_id_range} in stack {stack}")
                        render_api.save_tile_specs(stack=stack,
                                                   tile_specs=tile_specs,
                                                   derive_data=True)
                else:
                    logger.info(f'import_slab_stacks_for_wafer: ignoring {scan_path.name} for stack {stack}')

            renderapi.stack.set_stack_state(stack, 'COMPLETE', render=render)


def main(arg_list: List[str]):
    parser = argparse.ArgumentParser(
        description="Parse wafer metadata and convert to tile specs that can be saved to render.",
        parents=[build_wafer_info_parent_parser()]
    )
    parser.add_argument(
        "--render_host",
        help="Render web services host (e.g. em-services-1.int.janelia.org)",
        required=True,
    )
    parser.add_argument(
        "--render_owner",
        help="Owner for all created render stacks",
        required=True,
    )
    parser.add_argument(
        "--import_scan_name",
        help="If specified, build wafer info using all non-excluded scans but only derive and import "
             "tile specs for these scans (e.g. scan_001)",
        nargs='+',
        default=[]
    )
    args = parser.parse_args(args=arg_list)

    wafer_info = load_wafer_info(wafer_base_path=Path(args.wafer_base_path),
                                 number_of_slabs_per_group=args.number_of_slabs_per_render_project,
                                 slab_name_width=args.slab_name_width,
                                 exclude_scan_name_list=args.exclude_scan_name)

    import_slab_stacks_for_wafer(render_ws_host=args.render_host,
                                 render_owner=args.render_owner,
                                 wafer_info=wafer_info,
                                 import_scan_name_list=args.import_scan_name)


if __name__ == '__main__':
    main(sys.argv[1:])
