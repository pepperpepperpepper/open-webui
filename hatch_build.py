# noqa: INP001
import os
import shutil
import subprocess
from sys import stderr

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        super().initialize(version, build_data)
        if os.environ.get('OPEN_WEBUI_SKIP_FRONTEND_BUILD', '').lower() in {
            '1',
            'true',
            'yes',
        }:
            if os.path.isdir('build'):
                stderr.write(
                    '>>> Skipping Open Webui frontend build and reusing existing build/\n'
                )
                return
            raise RuntimeError(
                'OPEN_WEBUI_SKIP_FRONTEND_BUILD is set but no prebuilt build/ directory exists'
            )
        stderr.write('>>> Building Open Webui frontend\n')
        npm = shutil.which('npm')
        if npm is None:
            raise RuntimeError('NodeJS `npm` is required for building Open Webui but it was not found')
        stderr.write('### npm install\n')
        subprocess.run([npm, 'install', '--force'], check=True)  # noqa: S603
        stderr.write('\n### npm run build\n')
        os.environ['APP_BUILD_HASH'] = version
        subprocess.run([npm, 'run', 'build'], check=True)  # noqa: S603
