#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: Ceyhun Uzunoglu <ceyhunuzngl AT gmail [DOT] com>
"""Generates StepChain tasks' CPU efficiency static web site"""

import os
import time
from datetime import datetime, timedelta

import pandas as pd
from pyspark import SparkContext
from pyspark.sql import SparkSession
from pyspark.sql import functions as fn
from pyspark.sql.functions import col
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, DoubleType, LongType

# pd.set_option("display.max_colwidth", None)
pd.options.display.float_format = "{:,.3f}".format
pd.set_option("display.max_colwidth", -1)

kibana_link_1 = (
        '''<a target="_blank" title="First click can be SSO redirection. ''' +
        '''If so, please click 2nd time" href="''' +
        '''https://monit-kibana.cern.ch/kibana/app/kibana#/discover?_g=(filters:!(),refreshInterval:''' +
        '''(pause:!t,value:0),time:(from:'{START_DAY}',to:'{END_DAY}'))''' +
        '''&_a=(columns:!(_source),filters:!(('$state':(store:appState),meta:(alias:!n,disabled:!f,index''' +
        ''':'60770470-8326-11ea-88fc-cfaa9841e350',key:data.steps.site,negate:!f,params:(query:'''
)
# + SITE_NAME
kibana_link_2 = '''),type:phrase,value:'''
# + SITE_NAME
kibana_link_3 = '''),query:(match:(data.steps.site:(query:'''
# + SITE_NAME
kibana_link_4 = (
        ''',type:phrase))))),index:'60770470-8326-11ea-88fc-cfaa9841e350',interval:auto,query:''' +
        '''(language:lucene,query:'data.meta_data.jobstate:success%20AND%20data.meta_data.jobtype:''' +
        '''Production%20AND%20data.task:%22'''
)
# + TASK_NAME
kibana_link_5 = '''%22'),sort:!(metadata.timestamp,desc))">@Kibana</a>'''

_DEFAULT_HDFS_FOLDER = "/project/monitoring/archive/wmarchive/raw/metric"
_OUTPUT_FOLDER = "/eos/user/c/cmsmonit/www/stepchain"


def get_spark_session(yarn=True, verbose=False):
    """
    Get or create the spark context and session.
    """
    sc = SparkContext(appName="cms-stepchain-cpu-eff")
    return SparkSession.builder.config(conf=sc._conf).getOrCreate()


def get_candidate_files(
        start_date, end_date, spark, base=_DEFAULT_HDFS_FOLDER,
):
    """
    Returns a list of hdfs folders that can contain data for the given dates.
    """
    st_date = start_date - timedelta(days=1)
    ed_date = end_date + timedelta(days=1)
    days = (ed_date - st_date).days
    sc = spark.sparkContext
    # The candidate files are the folders to the specific dates,
    # but if we are looking at recent days the compaction procedure could
    # have not run yet so we will considerate also the .tmp folders.
    candidate_files = [
        f"{base}/{(st_date + timedelta(days=i)).strftime('%Y/%m/%d')}{{,.tmp}}"
        for i in range(0, days)
    ]
    FileSystem = sc._gateway.jvm.org.apache.hadoop.fs.FileSystem
    URI = sc._gateway.jvm.java.net.URI
    Path = sc._gateway.jvm.org.apache.hadoop.fs.Path
    fs = FileSystem.get(URI("hdfs:///"), sc._jsc.hadoopConfiguration())
    candidate_files = [url for url in candidate_files if fs.globStatus(Path(url))]
    return candidate_files


def schema():
    """Final schema of steps"""
    return StructType([
        StructField('ts', LongType(), nullable=False),
        StructField('task', StringType(), nullable=False),
        StructField('fwjr_id', StringType(), nullable=False),
        StructField('site', StringType(), nullable=False),
        StructField('acquisitionEra', StringType(), nullable=True),
        StructField('step_name', StringType(), nullable=False),
        StructField('jobCPU', DoubleType(), nullable=True),
        StructField('jobTime', DoubleType(), nullable=True),
        StructField('ncores', IntegerType(), nullable=True),
        StructField('nthreads', IntegerType(), nullable=True),
        StructField('era_len', IntegerType(), nullable=True),
        StructField('steps_len', IntegerType(), nullable=False),
        StructField('cpuEff', DoubleType(), nullable=True),
    ])


def udf_step_extract(row):
    """
    Borrowed from wmarchive.py

    Helper function to extract useful data from WMArchive records.
    Returns list of step_res
    """
    meta = row.meta_data
    result = []
    count = 0
    task_name = row.task
    for step in row['steps']:
        if step['name'].lower().startswith('cmsrun'):
            step_res = {'ts': meta.ts, 'task': task_name, 'fwjr_id': meta.fwjr_id}
            count += 1
            step_res["step_name"] = step.name
            step_res['site'] = step.site
            step_res['ncores'] = step.performance.cpu.NumberOfStreams
            step_res['nthreads'] = step.performance.cpu.NumberOfThreads
            step_res['jobCPU'] = step.performance.cpu.TotalJobCPU
            step_res['jobTime'] = step.performance.cpu.TotalJobTime
            if step_res['jobCPU'] and step_res['nthreads'] and step_res['jobTime']:
                step_res['cpuEff'] = round(100 * (step_res['jobCPU'] / step_res['nthreads']) / step_res['jobTime'],
                                           2)
            else:
                step_res['cpuEff'] = None
            step_res['acquisitionEra'] = set()
            if step.output:
                for outx in step.output:
                    step_res['acquisitionEra'].add(outx.acquisitionEra)
            if step_res['acquisitionEra']:
                step_res['era_len'] = len(step_res['acquisitionEra'])
                step_res['acquisitionEra'] = step_res['acquisitionEra'].pop()
            else:
                step_res['era_len'] = 0
                step_res['acquisitionEra'] = None
            result.append(step_res)
    if result:
        [r.setdefault("steps_len", count) for r in result]
        return result


def _generate_main_page(selected_pd, task_column, start_date, end_date):
    """Create HTML page

    Linux file name cannot contain slash character.
    Task names contains slash character and html file names consist of task names.
    Because of that, a workaround is applied: slash is replaced with "-_-" before writing htmls.
    Also this logic is used in JavaScript script to get html file content.
    """

    selected_pd["task"] = (
            f'<a class="taskname">'
            + task_column
            + '</a><br>'
    )
    _fc = '<a class="selname">' + task_column + "</a>"

    selected_pd[task_column.name] = _fc
    html = selected_pd.to_html(escape=False, index=False)
    html = html.replace(
        'table border="1" class="dataframe"',
        'table id="dataframe" class="display compact" style="width:100%;"',
    )
    html = html.replace('style="text-align: right;"', "")
    # cleanup of the default dump
    html = html.replace(
        'table border="1" class="dataframe"',
        'table id="dataframe" class="display compact" style="width:100%;"',
    )
    html = html.replace('style="text-align: right;"', "")

    html_header = f"""<!DOCTYPE html>
    <html>
    <head>
    <link rel="stylesheet" href="https://cdn.datatables.net/1.10.20/css/jquery.dataTables.min.css">
    <style>
    .dataTables_filter input {{
      border: 7px solid Tomato;
      width: 400px;
      font-size: 16px;
      font-weight: bold;
    }}
    table td {{
    word-break: break-all;
    }}
    #dataframe-tiers table {{
      font-family: arial, sans-serif;
      border-collapse: collapse;
      width: 100%;
    }}
    #dataframe-tiers td, th {{
      border: 1px solid #dddddd;
      text-align: left;
      padding: 8px;
    }}
    #dataframe-tiers tr:nth-child(even) {{
      background-color: #dddddd;
    }}
    </style>
    </head>
    <body>
    <h2>Dump of CMSSW StepChain CPU Efficiencies
    from {start_date.strftime("%A %d. %B %Y")} to {end_date.strftime("%A %d. %B %Y")}</h2>
     <ul>
      <li>"mean_cpueff" is the average of all individual steps(cmsRun1,2,etc.) of a task. </li>
      <li>In detailed view, "mean_cpueff" is the average cpu eff of a specific step type in a site. </li>
      <li>Individual step cpu efficiency calculation: <code>cpuEff=(jobCPU / nthreads) / jobTime </code> </li>
      <li>
        Ref1: <a href="https://github.com/dmwm/CMSSpark/blob/master/src/python/CMSSpark/stepchain_cpu_eff.py">
            Python script
        </a>
      </li>
    </ul>
    """
    # > Tiers table
    html_middle = (
        '''
        <div class="container" style="display:block; width:100%">
    ''')
    # > WF table
    html_footer = (
        '''
        </div>
        <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.3.1/jquery.min.js">
        </script>
        <script type="text/javascript" src="https://cdn.datatables.net/1.10.20/js/jquery.dataTables.min.js">
        </script>
        <script>
            $(document).ready(function () {
            function toggleDetails(){
                    var tr = $(this).closest("tr");
                    sel_name = $(tr).find("td a.selname").text()
                    task_name = $(tr).find("td a.task").text()
                    d_class="details-show"
                    row = dt.row(tr)
                    if(!row.child.isShown())
                    {
                        console.log(task_name)
                        // html file name of a task includes slash replaced with -_-, 
                        // so obey that rule to get html content                       
                        sel_name_replaced = sel_name.replaceAll(/\//g, "-_-");
                        $(tr).addClass(d_class)
                        row.child("<div id='details_"+sel_name_replaced+"'>loading</div>").show()
                        folder = "wfbytask"
                        console.log(folder+"/Cpu_Eff_byTask_"+sel_name_replaced+".html")
                        $.get(folder+"/Cpu_Eff_byTask_"+sel_name_replaced+".html", function (response){
                            var html = response;
                            $("#details_"+sel_name_replaced).html(html);
                        });
                    }else{
                        $(tr).removeClass(d_class)
                        row.child.hide()
                    }
                }
                $('table#dataframe thead tr').append('<th>task details</th>');
                $('table#dataframe tbody tr').append('<td><button class="btn-details">+</button></td>');
                var dt = $('#dataframe').DataTable( {
                    "order": [[ 4, "asc" ]],
                    "scrollX": false,
                    language: {
                        search: "_INPUT_",
                        searchPlaceholder: "--- Search Tasks ---",
                    },
                });
                $('table#dataframe tbody tr').on('click','td button.btn-details',toggleDetails)
                dt.on('draw', function(){
                $('table#dataframe tbody tr').off('click').on('click','td button.btn-details',toggleDetails)
                })
            });
        </script></body></html>'''
    )

    return html_header + html_middle + html + html_footer


def write_htmls(grouped_details, grouped_task, start_date, end_date, output_folder):
    """Write detailed dataframe and main page's task dataframe to html files

    Because of that Linux file name cannot contain slash,
    html file name of a task contains different characters(-_-) which are replaced with slash.
    This replacement trick is transparent, users see normal task names.
    """
    grouped_details = grouped_details.set_index(["task", "site", "step_name"]).sort_index()
    grouped_details["@Kibana"] = (
            kibana_link_1.format(START_DAY=(start_date + timedelta(seconds=time.altzone))
                                 .strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                 END_DAY=(end_date + timedelta(seconds=time.altzone))
                                 .strftime('%Y-%m-%dT%H:%M:%S.000Z')
                                 ) +
            grouped_details.index.get_level_values('site') +
            kibana_link_2 +
            grouped_details.index.get_level_values('site') +
            kibana_link_3 +
            grouped_details.index.get_level_values('site') +
            kibana_link_4 +
            grouped_details.index.get_level_values('task') +
            kibana_link_5
    )
    # Create one file per worflow, so we don't have a big file collapsing the browser.
    _folder = f"{output_folder}/wfbytask"
    os.makedirs(_folder, exist_ok=True)
    for task_name, df_iter in grouped_details.groupby(["task"]):
        task_name = task_name.replace("/", "-_-")
        df_iter.droplevel(["task"]).to_html(
            f"{_folder}/Cpu_Eff_byTask_{task_name}.html", escape=False,
        )

    task_column = grouped_task["task"].copy()
    main_page = _generate_main_page(grouped_task, task_column, start_date, end_date)
    os.makedirs(output_folder, exist_ok=True)
    with open(f"{output_folder}/CPU_Efficiency_Table.html", "w") as ofile:
        ofile.write(main_page)


def main():
    """Get step data in wmarchive.

    Each step array contains multiple steps. Udf function returns each step as a separate row in a list.
    flatMap helps to flat list of steps to become individual rows in dataframe.
    """
    start_date = datetime(2021, 7, 20)
    end_date = datetime(2021, 8, 4)
    spark = get_spark_session()
    df_raw = spark.read.option("basePath", _DEFAULT_HDFS_FOLDER).json(
        get_candidate_files(start_date, end_date, spark, base=_DEFAULT_HDFS_FOLDER)
    ) \
        .select(["data.*", "metadata.timestamp"]) \
        .filter(
        f"""data.meta_data.jobstate='success'
                  AND data.meta_data.jobtype='Production'
                  AND data.wmats >= {start_date.timestamp()}
                  AND data.wmats < {end_date.timestamp()}
                  """
    )
    df_rdd = df_raw.rdd.flatMap(lambda r: udf_step_extract(r))
    df = spark.createDataFrame(df_rdd, schema=schema()).dropDuplicates().where(col("ncores").isNotNull()).cache()
    df_details = df.groupby(["task", "site", "step_name"]).agg(
        fn.mean("cpuEff").alias("mean_cpueff"),
        fn.sum("ncores").alias("sum_ncores"),
        fn.sum("nthreads").alias("sum_nthreads"),
        fn.sum("jobCPU").alias("sum_jobCPU"),
        fn.sum("jobTime").alias("sum_jobTime"),
        fn.mean("steps_len").alias("mean_steps_len"),
        fn.collect_set("acquisitionEra").alias("acquisitionEra"),
    ).toPandas()
    df_task = df.groupby(["task"]).agg(
        fn.mean("cpuEff").alias("mean_cpueff"),
        fn.sum("ncores").alias("sum_ncores"),
        fn.sum("nthreads").alias("sum_nthreads"),
        fn.mean("steps_len").alias("mean_steps_len"),
    ).toPandas()
    write_htmls(df_details, df_task, start_date, end_date, _OUTPUT_FOLDER)


if __name__ == "__main__":
    main()
