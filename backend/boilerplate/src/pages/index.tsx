import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

const Index = () => {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-gray-50 p-4">
      <Card className="w-full max-w-md text-center shadow-lg border-t-4 border-blue-600">
        <CardHeader>
          <CardTitle className="text-3xl font-bold">GorillaBuilder</CardTitle>
          <CardDescription>Your AI-generated app is ready.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-muted-foreground">
            This is the default index page. Ask the agent to build something amazing!
          </p>
          <div className="flex justify-center gap-4">
            <Button onClick={() => alert("Button Clicked!")}>Click Me</Button>
            <Button variant="outline">Learn More</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default Index;