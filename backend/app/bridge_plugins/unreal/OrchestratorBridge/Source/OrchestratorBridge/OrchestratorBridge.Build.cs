using UnrealBuildTool;

public class OrchestratorBridge : ModuleRules
{
	public OrchestratorBridge(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"DeveloperSettings",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"UnrealEd",
			"Slate",
			"SlateCore",
			"InputCore",
			"ToolMenus",
			"ContentBrowser",
			"AssetTools",
			"AssetRegistry",
			"WebSockets",
			"HTTP",
			"Json",
			"Settings",
		});
	}
}
